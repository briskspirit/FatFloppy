import datetime
import math
import os
import tempfile

from PyQt6.QtCore import (QCoreApplication, QMimeData, QPointF, Qt, QThread,
                          QTimer, QUrl, pyqtSignal)
from PyQt6.QtGui import (QAction, QBrush, QDrag, QFont, QIcon, QPainter, QPen,
                         QPolygonF)
from PyQt6.QtWidgets import (QAbstractItemView, QDockWidget, QFileDialog,
                             QGraphicsEllipseItem, QGraphicsLineItem,
                             QGraphicsPolygonItem, QGraphicsScene,
                             QGraphicsView, QInputDialog, QLabel, QMainWindow,
                             QMessageBox, QProgressDialog, QToolBar,
                             QTreeWidget, QTreeWidgetItem, QHeaderView)

from controller import DiskController


class ResizableGraphicsView(QGraphicsView):
    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.app = parent
        self.setSceneRect(0, 0, self.width()-2, self.height()-2) # -2 fixes scrollbar appearance

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setSceneRect(0, 0, self.width()-2, self.height()-2)
        self.app.draw_disk_map()

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


class FileSystemNode:
    def __init__(self, name, size=0, is_dir=False, modified="N/A", attributes="-", parent=None):
        self.name = name
        self.size = size
        self.is_dir = is_dir
        self.modified = modified
        self.attributes = attributes
        self.parent = parent
        self.children = []

    def appendChild(self, child):
        self.children.append(child)


class OperationWorker(QThread):
    progress_signal = pyqtSignal(float)
    error_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(object)

    def __init__(self, operation):
        super().__init__()
        self.operation = operation
        self.result = None

    def run(self):
        try:
            # Pass the progress signal emitter as the callback
            self.result = self.operation(self.progress_signal.emit)
            self.finished_signal.emit(self.result)
        except Exception as e:
            self.error_signal.emit(str(e))


class FileBrowserApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.root_node = None
        self.current_node = None
        self.current_path = "/"
        self.current_head = 0
        self.busy_clusters = []
        self.free_space = 0
        self.total_space = 0
        self.controller = None
        self.initUI()

        # Set up application-wide monospaced font
        self.setup_fonts()

    def setup_fonts(self):
        """Setup application-wide monospaced font with fallbacks."""
        # List of monospaced fonts in order of preference
        monospace_fonts = [
            "Courier New",        # All platforms
            "DejaVu Sans Mono",   # Linux
            "Consolas",           # Windows
            "Menlo",              # macOS
            "Liberation Mono",    # Linux
            "Monaco",             # macOS
        ]

        # Create font with the first available font in the list
        self.app_font = QFont()
        self.app_font.setFamily(monospace_fonts[0])  # Start with first preference
        self.app_font.setStyleHint(QFont.StyleHint.Monospace)  # Hint to use monospace if first choice unavailable
        self.app_font.setFixedPitch(True)  # Ensure fixed pitch
        self.app_font.setPointSize(12)     # Set reasonable size

        # Set font for the entire application
        self.setFont(self.app_font)

        # Apply the font to specific widgets that might need explicit setting
        self.tree_widget.setFont(self.app_font)
        self.file_list.setFont(self.app_font)
        self.bpb_info.setFont(self.app_font)

        # Create a slightly larger font for headings and labels
        header_font = QFont(self.app_font)
        # header_font.setPointSize(11)
        header_font.setBold(True)

        # Apply to headers
        self.tree_widget.headerItem().setFont(0, header_font)
        for i in range(self.file_list.columnCount()):
            self.file_list.headerItem().setFont(i, header_font)

    def perform_with_progress(self, operation, title="Operation in Progress..."):
        # Create a modal progress dialog
        progress_dialog = QProgressDialog(title, None, 0, 100, self)
        progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        progress_dialog.setMinimumDuration(0)
        progress_dialog.setCancelButton(None)
        progress_dialog.show()

        # Initialize the worker with the operation
        worker = OperationWorker(operation)

        # Connect signals
        worker.progress_signal.connect(lambda p: progress_dialog.setValue(int(p * 100)))
        worker.error_signal.connect(
            lambda e: (QMessageBox.critical(self, "Error", e), progress_dialog.close())
        )
        worker.finished_signal.connect(
            lambda result: (setattr(self, '_operation_result', result), progress_dialog.close())
        )

        # Start the worker thread
        worker.start()

        # Run the event loop until the worker finishes
        while worker.isRunning():
            QCoreApplication.processEvents()

        # Retrieve and return the result
        if hasattr(self, '_operation_result'):
            result = self._operation_result
            del self._operation_result
            return result
        return None

    def build_fs_tree(self):
        """Builds a tree representation of the filesystem."""
        if not self.controller:
            return None

        # Create the root node
        root_node = FileSystemNode("Root", is_dir=True, attributes="-")

        # Get all files for each directory
        all_directories = ["/"]
        directory_contents = {}

        # First, get all directories
        root_items = self.controller.list_directory("/")
        for item in root_items:
            if item["is_dir"]:
                path = "/" + item["name"]
                all_directories.append(path)

        # Process each directory
        for directory in all_directories:
            directory_contents[directory] = self.controller.list_directory(directory)

            # Find subdirectories and add them to the list
            for item in directory_contents[directory]:
                if item["is_dir"]:
                    if directory == "/":
                        path = f"/{item['name']}"
                    else:
                        path = f"{directory}/{item['name']}"
                    if path not in all_directories:
                        all_directories.append(path)

        # Create a dictionary to store all nodes by path
        node_dict = {"/": root_node}

        # First add all directories to ensure parent directories exist
        all_dirs = []
        for dir_path in all_directories:
            if dir_path == "/":
                continue

            parts = dir_path.strip("/").split("/")
            parent_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"

            # Find the directory info
            dir_info = None
            dir_name = parts[-1]
            for item in directory_contents.get(parent_path, []):
                if item["is_dir"] and item["name"] == dir_name:
                    dir_info = item
                    break

            if not dir_info:
                continue

            all_dirs.append({
                "path": dir_path,
                "parent_path": parent_path,
                "name": dir_name,
                "info": dir_info
            })

        # Sort directories by depth
        all_dirs.sort(key=lambda x: len(x["path"].split("/")))

        # Create directory nodes
        for dir_data in all_dirs:
            path = dir_data["path"]
            parent_path = dir_data["parent_path"]
            name = dir_data["name"]
            dir_info = dir_data["info"]

            # Skip if this is a duplicate entry
            full_path = (parent_path + "/" + name).replace("//", "/")
            if full_path in node_dict:
                continue

            # Get the parent node
            parent = node_dict.get(parent_path)
            if not parent:
                continue  # Skip if parent not found

            # Create the directory node
            node = FileSystemNode(
                name=name,
                size=0,
                is_dir=True,
                modified=dir_info["datetime"].strftime("%Y-%m-%d %H:%M:%S") if isinstance(dir_info["datetime"], datetime.datetime) else str(dir_info["datetime"]),
                attributes=dir_info["attributes"],
                parent=parent
            )

            # Add to parent's children
            parent.appendChild(node)

            # Add to node dictionary
            node_dict[path] = node

        # Then add all files
        for dir_path, items in directory_contents.items():
            parent = node_dict.get(dir_path)
            if not parent:
                continue

            for item in items:
                if not item["is_dir"]:
                    # Create the file node
                    node = FileSystemNode(
                        name=item["name"],
                        size=item["size"],
                        is_dir=False,
                        modified=item["datetime"].strftime("%Y-%m-%d %H:%M:%S") if isinstance(item["datetime"], datetime.datetime) else str(item["datetime"]),
                        attributes=item["attributes"],
                        parent=parent
                    )

                    # Add to parent's children
                    parent.appendChild(node)

        return root_node

    def initUI(self):
        self.setWindowTitle("FatFloppy - FAT12 Disk Browser")
        self.setGeometry(100, 100, 1200, 800)

        # Set window icon
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'icons', 'floppy_icon.png')
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        # Menu Bar - with style adjustments
        menu_bar = self.menuBar()
        menu_bar.setStyleSheet("QMenuBar { min-height: 20px; max-height: 25px; }")
        file_menu = menu_bar.addMenu("File")
        file_menu.setStyleSheet("QMenu { padding: 5px; }")

        open_image_action = QAction("Open Disk Image File", self)
        open_image_action.triggered.connect(self.open_disk_image_file)
        file_menu.addAction(open_image_action)

        open_floppy_action = QAction("Open Physical Floppy", self)
        open_floppy_action.triggered.connect(self.open_physical_floppy)
        file_menu.addAction(open_floppy_action)

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Main Toolbar
        self.toolbar = QToolBar("Main Toolbar", self)
        self.toolbar.setStyleSheet("QToolBar { spacing: 5px; min-height: 25px; max-height: 30px; }")
        self.addToolBar(self.toolbar)

        # Head selection action
        self.head_action = QAction("Switch to head 1", self)
        self.head_action.setToolTip("Switch between disk heads (sides)")
        self.head_action.triggered.connect(self.toggle_head)
        self.toolbar.addAction(self.head_action)
        self.toolbar.addSeparator()

        # Extract file action
        extract_action = QAction("Extract", self)
        extract_action.setToolTip("Extract selected file to local filesystem")
        extract_action.triggered.connect(self.extract_selected_file)
        self.toolbar.addAction(extract_action)

        # Delete item action
        delete_action = QAction("Delete", self)
        delete_action.setToolTip("Delete selected file or directory")
        delete_action.triggered.connect(self.delete_selected_item)
        self.toolbar.addAction(delete_action)

        # Create directory action
        create_dir_action = QAction("New Folder", self)
        create_dir_action.setToolTip("Create a new directory in current location")
        create_dir_action.triggered.connect(self.create_directory)
        self.toolbar.addAction(create_dir_action)

        # Add file action
        add_file_action = QAction("Add File", self)
        add_file_action.setToolTip("Add a file to current directory")
        add_file_action.triggered.connect(self.add_file)
        self.toolbar.addAction(add_file_action)

        # Directory Tree Dock
        self.tree_dock = QDockWidget("Directory Tree", self)
        self.tree_widget = QTreeWidget()
        self.tree_widget.setHeaderLabel("Directories")
        self.tree_widget.itemClicked.connect(self.select_directory)
        self.tree_dock.setWidget(self.tree_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.tree_dock)

        # BPB Information Dock
        self.bpb_dock = QDockWidget("BPB Information", self)
        self.bpb_info = QLabel("No disk image loaded")
        self.bpb_dock.setWidget(self.bpb_info)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.bpb_dock)

        self.splitDockWidget(self.tree_dock, self.bpb_dock, Qt.Orientation.Vertical)
        self.resizeDocks([self.tree_dock, self.bpb_dock], [640, 160], Qt.Orientation.Vertical)

        # File List Dock
        self.file_list_dock = QDockWidget("Files in Current Directory", self)
        self.file_list = DragDropTreeWidget(self)
        self.file_list.setHeaderLabels(["Name", "Size", "Date/Time", "Attr"])
        self.file_list.setDragEnabled(True)
        self.file_list.setAcceptDrops(True)
        self.file_list.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_list_dock.setWidget(self.file_list)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.file_list_dock)

        # Configure column widths
        header = self.file_list.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)          # Name column
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents) # Size column
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch) # Date/Time column
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch) # Attributes column

        # Disk Map Dock
        self.disk_map_dock = QDockWidget("Disk Map", self)
        self.disk_map_scene = QGraphicsScene()
        self.disk_map_view = ResizableGraphicsView(self.disk_map_scene, self)
        self.disk_map_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.disk_map_dock.setWidget(self.disk_map_view)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.disk_map_dock)

        self.splitDockWidget(self.file_list_dock, self.disk_map_dock, Qt.Orientation.Horizontal)
        self.resizeDocks([self.file_list_dock, self.disk_map_dock], [480, 720], Qt.Orientation.Horizontal)

        # Initialize UI displays
        self.reset_ui()

        self.statusBar().showMessage("Ready")

    def reset_ui(self):
        """Reset UI to initial empty state"""
        self.root_node = None
        self.current_node = None
        self.current_path = "/"
        self.current_head = 0
        self.busy_clusters = []
        self.free_space = 0
        self.total_space = 0

        if self.controller:
            self.controller.close_disk()
        self.controller = None

        self.tree_widget.clear()
        self.file_list.clear()
        self.bpb_info.setText("No disk image loaded")
        self.disk_map_scene.clear()
        self.disk_map_scene.addText("No disk image loaded").setPos(10, 10)

        self.head_action.setEnabled(False)
        self.head_action.setText("Switch to Head 1")

        self.statusBar().showMessage("Ready")

    def refresh_filesystem_ui(self, preserve_path=None):
        """
        Centralized method to refresh all filesystem-related UI components

        Args:
            preserve_path (str, optional): Path to navigate to after refresh.
                                          If None, defaults to root.
        """
        # Rebuild the tree from the filesystem
        self.root_node = self.build_fs_tree()
        self.populate_tree()

        # Restore the current directory selection if path provided
        if preserve_path:
            self.navigate_to_path(preserve_path)
        else:
            self.current_node = self.root_node
            self.current_path = "/"
            self.update_file_list()

        # Update space usage information
        self.get_busy_clusters()

        # Update BPB info with current free space
        self.update_bpb_info()

        # Redraw the disk map
        self.draw_disk_map()

        # Update status bar
        self.statusBar().showMessage(f"Current path: {self.current_path}")

    def open_disk_image_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Open Disk Image", "", "Disk Images (*.ima *.img)")
        if not file_path:
            return
        try:
            self.controller = DiskController()
            if self.controller.open_disk(file_path, "image"):
                self.root_node = self.build_fs_tree()
                self.current_node = self.root_node
                self.current_path = "/"
                self.refresh_filesystem_ui()

                # Check if disk has multiple heads
                if self.controller.disk and self.controller.disk.geometry and self.controller.disk.geometry.heads > 1:
                    self.head_action.setEnabled(True)
                    self.head_action.setText(f"Switch to Head {1 - self.current_head}")
                else:
                    self.head_action.setEnabled(False)
                    self.head_action.setText("Single-sided disk")

                self.statusBar().showMessage(f"Loaded: {file_path}")
            else:
                self.reset_ui()
                QMessageBox.critical(self, "Error", "Failed to open disk image")
        except Exception as e:
            self.reset_ui()
            QMessageBox.critical(self, "Error", f"Failed to open disk image: {str(e)}")

    def open_physical_floppy(self):
        try:
            device_name, ok = QInputDialog.getText(self, "Device Selection",
                                                "Enter device name (e.g., COM3):")
            if not ok or not device_name:
                device_name = None

            self.controller = DiskController()
            if self.controller.open_disk(device_name, "physical"):
                self.root_node = self.build_fs_tree()
                self.current_node = self.root_node
                self.current_path = "/"
                self.refresh_filesystem_ui()

                format_name = self.controller.detect_format()
                format_text = f" using {format_name}" if format_name else ""

                # Check if disk has multiple heads
                if self.controller.disk and self.controller.disk.geometry and self.controller.disk.geometry.heads > 1:
                    self.head_action.setEnabled(True)
                    self.head_action.setText(f"Switch to Head {1 - self.current_head}")
                else:
                    self.head_action.setEnabled(False)
                    self.head_action.setText("Single-sided disk")

                self.statusBar().showMessage(f"Loaded physical floppy{format_text}")
            else:
                self.reset_ui()
                QMessageBox.critical(self, "Error", "Failed to open physical floppy")
        except Exception as e:
            self.reset_ui()
            QMessageBox.critical(self, "Error", f"Failed to open physical floppy: {str(e)}")

    def update_bpb_info(self):
        if not self.controller or not self.controller.filesystem:
            self.bpb_info.setText("No disk image loaded")
            return

        if not self.controller.disk or not self.controller.disk.geometry:
            self.bpb_info.setText("Disk geometry not available")
            return

        geometry = self.controller.disk.geometry
        sector_size = geometry.sector_size
        total_sectors = geometry.total_sectors

        # Try to get free space
        space_info = self.controller.get_free_space()
        if space_info:
            free_bytes, total_bytes = space_info
            free_kb = free_bytes / 1024
            total_kb = total_bytes / 1024
            percent_free = (free_bytes / total_bytes * 100) if total_bytes > 0 else 0
        else:
            total_bytes = total_sectors * sector_size
            free_kb = 0
            total_kb = total_bytes / 1024
            percent_free = 0

        # Get filesystem type
        fs_type = self.controller.detect_filesystem() or "Unknown"

        # Get format information
        format_info = "Unknown"
        format_name = self.controller.detect_format()
        if format_name:
            format_profile = self.controller.format_manager.get_format_by_name(format_name)
            if format_profile:
                format_info = format_profile.description

        # Build info text
        info = (
            f"Bytes per Sector: {sector_size}\n"
            f"Sectors per Track: {geometry.sectors_per_track}\n"
            f"Number of Heads: {geometry.heads}\n"
            f"Number of Tracks: {geometry.cylinders}\n"
            f"Total Sectors: {total_sectors}\n"
            f"Format: {format_info}\n"
            f"Filesystem: {fs_type}\n"
            f"Free Space: {free_kb:.1f} KB / {total_kb:.1f} KB ({percent_free:.1f}%)"
        )

        self.bpb_info.setText(info)

    def populate_tree(self):
        """Populate the directory tree widget"""
        self.tree_widget.clear()
        if not self.root_node:
            return

        root_item = QTreeWidgetItem(self.tree_widget, ["Root"])
        root_item.node = self.root_node
        self._populate_tree(self.root_node, root_item)
        self.tree_widget.expandAll()

    def _populate_tree(self, node, parent_item):
        """Recursively populate tree items for directories"""
        if not node:
            return

        for child in node.children:
            if child.is_dir:
                child_item = QTreeWidgetItem(parent_item, [child.name])
                child_item.node = child
                self._populate_tree(child, child_item)

    def select_directory(self, item):
        """Handle selection of directory in tree view"""
        self.current_node = item.node
        self.current_path = self.build_path_from_node(item.node)
        self.update_file_list()
        self.statusBar().showMessage(f"Viewing: {self.current_path}")

    def build_path_from_node(self, node):
        """Build full path from a node by traversing up to root"""
        path_parts = []
        curr = node
        while curr and curr.parent:  # Don't include "Root" in the path
            path_parts.insert(0, curr.name)
            curr = curr.parent
        return "/" + "/".join(path_parts) if path_parts else "/"

    def update_file_list(self):
        """Update file list view with current directory contents"""
        self.file_list.clear()
        if not self.current_node:
            return

        for child in self.current_node.children:
            item = QTreeWidgetItem(self.file_list, [
                child.name,
                str(child.size) if not child.is_dir else "",
                child.modified,
                child.attributes
            ])
            item.node = child

    def extract_selected_file(self):
        selected_items = self.file_list.selectedItems()
        if not selected_items or not self.controller:
            return

        item = selected_items[0]
        node = item.node

        if node.is_dir:
            QMessageBox.information(self, "Info", "Cannot extract directories")
            return

        try:
            file_path = self.build_full_path(node.name)
            file_data = self.perform_with_progress(
                lambda cb: self.controller.read_file(file_path),
                title="Extracting File..."
            )

            if file_data:
                save_path, _ = QFileDialog.getSaveFileName(self, "Save File", node.name)
                if save_path:
                    with open(save_path, 'wb') as f:
                        f.write(file_data)
                    self.statusBar().showMessage(f"Extracted {node.name} to {save_path}")
            else:
                QMessageBox.warning(self, "Warning", "Failed to read file data")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to extract file: {str(e)}")

    def delete_selected_item(self):
        """Delete selected file or directory"""
        selected_items = self.file_list.selectedItems()
        if not selected_items or not self.controller:
            return

        item = selected_items[0]
        node = item.node

        try:
            # Build the full path to the file/directory
            item_path = self.build_full_path(node.name)

            # Confirm deletion
            msg_type = "directory" if node.is_dir else "file"
            if QMessageBox.question(self, "Confirm Deletion",
                                  f"Are you sure you want to delete the {msg_type} {node.name}?",
                                  QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                # Delete the item
                success = self.perform_with_progress(
                    lambda cb: self.controller.delete_item(item_path),
                    title="Deleting Item..."
                )

                if success:
                    # Remember the current path
                    current_path = self.current_path

                    # Update UI
                    self.refresh_filesystem_ui(current_path)

                    self.statusBar().showMessage(f"Deleted {node.name}")
                else:
                    QMessageBox.warning(self, "Warning", f"Failed to delete {node.name}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to delete item: {str(e)}")

    def navigate_to_path(self, path):
        """Navigate to the specified path and update the current node."""
        if path == "/":
            self.current_node = self.root_node
            self.current_path = "/"
            self.update_file_list()
            return True

        parts = path.strip("/").split("/")
        current = self.root_node

        for part in parts:
            found = False
            for child in current.children:
                if child.is_dir and child.name == part:
                    current = child
                    found = True
                    break
            if not found:
                # Path no longer exists, stay at the root
                self.current_node = self.root_node
                self.current_path = "/"
                self.update_file_list()
                return False

        self.current_node = current
        self.current_path = path
        self.update_file_list()

        # Also select the corresponding item in the tree widget
        self.select_tree_item_by_path(path)

        return True

    def select_tree_item_by_path(self, path):
        """Select the tree item corresponding to the given path."""
        if path == "/":
            # Select the root item
            if self.tree_widget.topLevelItemCount() > 0:
                self.tree_widget.setCurrentItem(self.tree_widget.topLevelItem(0))
            return

        parts = path.strip("/").split("/")
        if self.tree_widget.topLevelItemCount() == 0:
            return

        item = self.tree_widget.topLevelItem(0)  # Root item

        for part in parts:
            found = False
            for i in range(item.childCount()):
                child = item.child(i)
                if child.text(0) == part:
                    item = child
                    found = True
                    break
            if not found:
                return

        self.tree_widget.setCurrentItem(item)

    def create_directory(self):
        """Create a new directory in the current location"""
        if not self.controller or not self.current_node:
            QMessageBox.warning(self, "Warning", "No disk image loaded")
            return

        # Get current path
        current_path = self.current_path

        # Get new directory name from user
        dir_name, ok = QInputDialog.getText(self, "Create New Directory",
                                          "Enter directory name (8.3 format):")
        if not ok or not dir_name:
            return

        try:
            success = self.perform_with_progress(
                lambda cb: self.controller.create_directory(current_path + "/" + dir_name),
                title="Creating Directory..."
            )

            if success:
                # Update UI
                self.refresh_filesystem_ui(current_path)
                self.statusBar().showMessage(f"Created directory {dir_name} in {current_path}")
            else:
                QMessageBox.warning(self, "Warning", f"Failed to create directory {dir_name}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to create directory: {str(e)}")

    def format_83_filename(self, filename):
        """Format a filename to comply with 8.3 naming convention"""
        # Trim to 8.3 format if needed
        if len(filename) > 12 or filename.count('.') > 1:
            parts = filename.split('.')
            if len(parts) > 1:
                return parts[0][:8] + '.' + parts[-1][:3]
            else:
                return parts[0][:8]
        return filename.upper()

    def build_full_path(self, name):
        """Build a full path by combining current_path with a name"""
        path = self.current_path
        if path != "/":
            path += "/"
        return path + name

    def add_file(self):
        """Add a file to the current directory"""
        if not self.controller or not self.current_node:
            QMessageBox.warning(self, "Warning", "No disk image loaded")
            return

        # Get file to add
        file_path, _ = QFileDialog.getOpenFileName(self, "Select File to Add")
        if not file_path:
            return

        # Get destination filename (8.3 format)
        base_name = os.path.basename(file_path)
        base_name = self.format_83_filename(base_name)

        new_name, ok = QInputDialog.getText(self, "File Name",
                                          "Enter file name (8.3 format):",
                                          text=base_name)
        if not ok or not new_name:
            return

        try:
            self.add_file_to_disk(file_path, new_name, self.current_path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to add file: {str(e)}")

    def add_file_to_disk(self, file_path, dest_name, dest_path):
        """Add a file to the disk with error handling"""
        # Read file data
        with open(file_path, 'rb') as f:
            file_data = f.read()

        # Add file to disk
        full_path = f"{dest_path}{'/' if not dest_path.endswith('/') else ''}{dest_name}"
        success = self.perform_with_progress(
            lambda cb: self.controller.write_file(full_path, file_data),
            title="Adding File..."
        )

        if success:
            # Update UI
            self.refresh_filesystem_ui(dest_path)
            self.statusBar().showMessage(f"Added file {dest_name} to {dest_path}")
        else:
            QMessageBox.warning(self, "Warning", f"Failed to add file {dest_name}")

    def generate_arc_points(self, x0, y0, radius, theta_start, theta_end, num_points):
        """Generate points along an arc for polygon drawing."""
        points = []
        delta_theta = (theta_end - theta_start) / (num_points - 1)
        for i in range(num_points):
            theta = theta_start + i * delta_theta
            x = x0 + radius * math.cos(theta)
            y = y0 + radius * math.sin(theta)
            points.append(QPointF(x, y))
        return points

    def toggle_head(self):
        """Toggle between disk heads/sides"""
        if self.controller and self.controller.disk and self.controller.disk.geometry and self.controller.disk.geometry.heads > 1:
            self.current_head = 1 - self.current_head
            self.head_action.setText(f"Switch to Head {1 - self.current_head}")
            self.draw_disk_map()
        else:
            QMessageBox.information(self, "Info", "Head switching is not available for this disk.")

    def get_busy_clusters(self):
        """Identify busy clusters and calculate free space"""
        if not self.controller:
            self.busy_clusters = []
            self.free_space = 0
            self.total_space = 0
            return

        try:
            # Get allocated clusters
            self.busy_clusters = self.controller.get_allocated_clusters()

            # Get free space information from controller
            space_info = self.controller.get_free_space()
            if space_info:
                free_bytes, total_bytes = space_info
                # Convert to clusters for visualization
                if self.controller.disk and self.controller.disk.geometry:
                    sector_size = self.controller.disk.geometry.sector_size
                    sectors_per_cluster = 1
                    if self.controller.filesystem and hasattr(self.controller.filesystem, "boot_sector"):
                        sectors_per_cluster = self.controller.filesystem.boot_sector.sectors_per_cluster

                    self.free_space = free_bytes // (sector_size * sectors_per_cluster)
                    self.total_space = total_bytes // (sector_size * sectors_per_cluster)
                else:
                    self.free_space = free_bytes // 512  # Fallback to default sector size
                    self.total_space = total_bytes // 512
            else:
                # If free space info not available, calculate from busy clusters
                if self.controller.disk and self.controller.disk.geometry:
                    geometry = self.controller.disk.geometry
                    total_sectors = geometry.total_sectors
                    sectors_per_cluster = 1
                    if self.controller.filesystem and hasattr(self.controller.filesystem, "boot_sector"):
                        sectors_per_cluster = self.controller.filesystem.boot_sector.sectors_per_cluster

                    # Calculate total data clusters (exclude boot, FAT, root dir)
                    if hasattr(self.controller.filesystem, "boot_sector"):
                        bpb = self.controller.filesystem.boot_sector
                        reserved = bpb.reserved_sectors
                        fat_size = bpb.sectors_per_fat * bpb.num_fats
                        root_dir_sectors = (bpb.root_entries * 32 + bpb.bytes_per_sector - 1) // bpb.bytes_per_sector
                        data_sectors = total_sectors - reserved - fat_size - root_dir_sectors
                        self.total_space = data_sectors // sectors_per_cluster
                    else:
                        # Rough estimate if no BPB available
                        self.total_space = (total_sectors - 33) // sectors_per_cluster  # 33 is typical overhead

                    # Calculate free space
                    self.free_space = self.total_space - len(self.busy_clusters)

        except Exception as e:
            self.busy_clusters = []
            self.free_space = 0
            self.total_space = 0
            print(f"Error getting busy clusters: {e}")

    def draw_disk_map(self):
        """Draw a visual representation of the disk's layout"""
        self.disk_map_scene.clear()

        if not self.controller or not self.controller.disk:
            view_width = self.disk_map_view.width()
            view_height = self.disk_map_view.height()
            text = self.disk_map_scene.addText("No disk image loaded")
            text.setFont(self.app_font)
            text_width = text.boundingRect().width()
            text_height = text.boundingRect().height()
            text.setPos((view_width - text_width) / 2, (view_height - text_height) / 2)
            return

        try:
            # Check if the current head is valid for the disk
            if self.controller.disk.geometry and self.current_head >= self.controller.disk.geometry.heads:
                self.disk_map_scene.addText("No data for this head").setPos(10, 10)
                return

            view_width = self.disk_map_view.width()
            view_height = self.disk_map_view.height()
            x0 = view_width / 2
            y0 = view_height / 2
            r_min = min(view_width, view_height) * 0.1
            r_max = min(view_width, view_height) * 0.45

            # Get disk geometry
            if not self.controller.disk.geometry:
                self.disk_map_scene.addText("Disk geometry not available").setPos(10, 10)
                return

            geometry = self.controller.disk.geometry
            sectors_per_track = geometry.sectors_per_track
            num_heads = geometry.heads
            sector_size = geometry.sector_size
            total_sectors = geometry.total_sectors
            num_cylinders = geometry.cylinders

            # Extract filesystem parameters needed for drawing
            bpb = None
            if self.controller.filesystem and hasattr(self.controller.filesystem, "boot_sector"):
                bpb = self.controller.filesystem.boot_sector

            # Set FAT filesystem parameters
            if bpb and hasattr(bpb, "reserved_sectors"):
                reserved_sectors = bpb.reserved_sectors
                num_fats = bpb.num_fats
                fat_size = bpb.sectors_per_fat
                root_entries = bpb.root_entries
                sectors_per_cluster = bpb.sectors_per_cluster
                root_dir_sectors = (root_entries * 32 + sector_size - 1) // sector_size
                fat_start = reserved_sectors
                root_dir_start = fat_start + (num_fats * fat_size)
                data_area_start = root_dir_start + root_dir_sectors
                first_data_sector = data_area_start
            else:
                # Default values for FAT12
                reserved_sectors = 1
                num_fats = 2
                fat_size = 9
                root_dir_sectors = 14
                sectors_per_cluster = 1
                fat_start = reserved_sectors
                root_dir_start = fat_start + (num_fats * fat_size)
                first_data_sector = root_dir_start + root_dir_sectors

            # Calculate angle for each sector
            angle_per_sector = 360 / sectors_per_track
            num_points = 20

            # Draw legend
            legend_x = 10
            legend_y = 10
            square_size = 10
            vertical_spacing = 10
            colors = [
                ("Boot Sector", Qt.GlobalColor.red),
                ("FAT1", Qt.GlobalColor.green),
                ("FAT2", Qt.GlobalColor.blue),
                ("Root Directory", Qt.GlobalColor.yellow),
                ("Busy Data Sector", Qt.GlobalColor.magenta),
                ("Free Data Sector", Qt.GlobalColor.gray),
            ]

            for i, (label, color) in enumerate(colors):
                # Create and add the square
                rect = QGraphicsPolygonItem(QPolygonF([
                    QPointF(legend_x, legend_y + i * vertical_spacing),
                    QPointF(legend_x + square_size, legend_y + i * vertical_spacing),
                    QPointF(legend_x + square_size, legend_y + i * vertical_spacing + square_size),
                    QPointF(legend_x, legend_y + i * vertical_spacing + square_size)
                ]))
                rect.setBrush(QBrush(color))
                self.disk_map_scene.addItem(rect)

                # Create and add the text, centering it vertically with the square
                text = self.disk_map_scene.addText(label)
                text.setFont(self.app_font)
                text_height = text.boundingRect().height()
                y_offset = (square_size - text_height) / 2
                text.setPos(legend_x + square_size + 5, legend_y + i * vertical_spacing + y_offset)

            # Draw statistics
            stats_x = 10
            stats_y = view_height - 60
            stats_text = self.disk_map_scene.addText(
                f"Head: {self.current_head}\n"
                f"Total: {self.total_space} clusters\n"
                f"Free: {self.free_space} clusters\n"
                f"Used: {self.total_space - self.free_space} clusters"
            )
            stats_text.setPos(stats_x, stats_y)
            stats_text.setFont(self.app_font)

            # Draw sectors - one cylinder at a time
            for c in range(num_cylinders):
                cyl_start_sector = c * sectors_per_track * num_heads + (self.current_head * sectors_per_track)

                for i in range(sectors_per_track):
                    s = cyl_start_sector + i

                    if s < total_sectors:
                        color = self.get_sector_color(s, sectors_per_cluster, reserved_sectors, fat_size, root_dir_sectors, first_data_sector)

                        theta_start = math.radians(i * angle_per_sector)
                        theta_end = math.radians((i + 1) * angle_per_sector)
                        r_outer = r_max - (r_max - r_min) * c / num_cylinders
                        r_inner = r_max - (r_max - r_min) * (c + 1) / num_cylinders

                        inner_points = self.generate_arc_points(x0, y0, r_inner, theta_start, theta_end, num_points)
                        outer_points = self.generate_arc_points(x0, y0, r_outer, theta_end, theta_start, num_points)
                        points = inner_points + outer_points

                        polygon = QGraphicsPolygonItem(QPolygonF(points))
                        polygon.setBrush(QBrush(color))
                        polygon.setPen(QPen(Qt.GlobalColor.black, 0.5))
                        self.disk_map_scene.addItem(polygon)

            # Draw radial lines and concentric circles
            for sector in range(sectors_per_track):
                theta = math.radians(sector * angle_per_sector)
                p1 = QPointF(x0 + r_min * math.cos(theta), y0 + r_min * math.sin(theta))
                p2 = QPointF(x0 + r_max * math.cos(theta), y0 + r_max * math.sin(theta))
                line = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
                line.setPen(QPen(Qt.GlobalColor.black, 0.5))
                self.disk_map_scene.addItem(line)

            for cylinder in range(1, num_cylinders):
                r = r_max - (r_max - r_min) * cylinder / num_cylinders
                ellipse = QGraphicsEllipseItem(x0 - r, y0 - r, 2 * r, 2 * r)
                ellipse.setPen(QPen(Qt.GlobalColor.black, 0.5))
                ellipse.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                self.disk_map_scene.addItem(ellipse)

        except Exception as e:
            self.disk_map_scene.addText(f"Error drawing disk map: {str(e)}").setPos(10, 10)
            import traceback
            traceback.print_exc()

    def get_sector_color(self, sector_num, sectors_per_cluster, reserved, fat_size, root_dir_sectors, first_data_sector):
        """Determine the color for a sector based on its role in FAT12 filesystem."""
        if sector_num < reserved:
            return Qt.GlobalColor.red  # Boot sector and reserved
        elif sector_num < reserved + fat_size:
            return Qt.GlobalColor.green  # FAT1
        elif sector_num < reserved + 2 * fat_size:
            return Qt.GlobalColor.blue  # FAT2
        elif sector_num < first_data_sector:
            return Qt.GlobalColor.yellow  # Root directory
        else:
            # Data area: color based on cluster status
            try:
                relative_sector = sector_num - first_data_sector
                cluster = (relative_sector // sectors_per_cluster) + 2  # Cluster numbers start at 2
                return Qt.GlobalColor.magenta if cluster in self.busy_clusters else Qt.GlobalColor.gray
            except Exception as e:
                print(f"Error determining cluster for sector {sector_num}: {e}")
                return Qt.GlobalColor.lightGray


def run_gui():
    import sys
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName("FatFloppy")
    window = FileBrowserApp()
    window.show()
    sys.exit(app.exec())
