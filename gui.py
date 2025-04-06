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

from diskmanager import FloppyDiskManager, ImageFileManager
from fat import FileSystemFactory


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
        if not hasattr(self.parent, 'fs') or not self.parent.current_node:
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
                    file_data = self.parent.fs.extract_file(file_path)
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
        self.fs = None
        self.bpb = None
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
        if not hasattr(self, 'fs') or self.fs is None:
            return None

        # Create the root node
        root_node = FileSystemNode("Root", is_dir=True, attributes="-")

        # Get all files and directories from the filesystem
        files = self.fs.list_files()

        # Create a dictionary to store all nodes by path
        node_dict = {"/": root_node}

        # First add all directories to ensure parent directories exist
        all_dirs = [f for f in files if f['is_dir']]
        all_dirs.sort(key=lambda x: len(x['name'].split('/')))  # Sort by path depth

        for file in all_dirs:
            path = file['name']
            parts = path.strip("/").split("/")
            parent_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
            name = parts[-1]

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
                modified=file['datetime'].strftime("%Y-%m-%d %H:%M:%S"),
                attributes=file['attributes'],
                parent=parent
            )

            # Add to parent's children
            parent.appendChild(node)

            # Add to node dictionary
            node_dict[full_path] = node

        # Then add all files
        all_files = [f for f in files if not f['is_dir']]
        for file in all_files:
            path = file['name']
            parts = path.strip("/").split("/")
            parent_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
            name = parts[-1]

            # Get the parent node
            parent = node_dict.get(parent_path)
            if not parent:
                continue  # Skip if parent not found

            # Create the file node
            node = FileSystemNode(
                name=name,
                size=file['size'],
                is_dir=False,
                modified=file['datetime'].strftime("%Y-%m-%d %H:%M:%S"),
                attributes=file['attributes'],
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
        self.fs = None
        self.bpb = None
        self.disk_manager = None

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
            self.disk_manager = ImageFileManager(file_path)
            self.fs = FileSystemFactory.create_filesystem(
                read_bytes_func=self.disk_manager.read_bytes,
                write_bytes_func=self.disk_manager.write_bytes,
                flush_func=self.disk_manager.flush
            )
            bpb = self.fs.get_bpb_info()
            self.disk_manager.sectors_per_track = bpb['sectors_per_track']
            self.disk_manager.num_heads = bpb['num_heads']
            self.disk_manager.sector_size = bpb['bytes_per_sector']
            self.disk_manager.total_sectors = bpb['total_sectors']
            self.disk_manager.num_cylinders = self.disk_manager.total_sectors // (self.disk_manager.sectors_per_track * self.disk_manager.num_heads)
            self.root_node = self.build_fs_tree()
            self.current_node = self.root_node
            self.current_path = "/"
            self.refresh_filesystem_ui()
            if self.disk_manager.num_heads > 1:
                self.head_action.setEnabled(True)
                self.head_action.setText(f"Switch to Head {1 - self.current_head}")
            else:
                self.head_action.setEnabled(False)
                self.head_action.setText("Single-sided disk")
            self.statusBar().showMessage(f"Loaded: {file_path}")
        except Exception as e:
            self.reset_ui()
            QMessageBox.critical(self, "Error", f"Failed to open disk image: {str(e)}")

    def open_physical_floppy(self):
        try:
            device_name, ok = QInputDialog.getText(self, "Device Selection",
                                                "Enter device name (e.g., COM3):")
            if not ok or not device_name:
                device_name = None
            format_name, ok = QInputDialog.getText(self, "Format Selection",
                                                "Enter format name (e.g., ibm.1440, ibm.scan):",
                                                text="ibm.scan")
            if not ok or not format_name:
                format_name = "ibm.scan"
            self.disk_manager = FloppyDiskManager(device_name, format_name=format_name)
            self.fs = FileSystemFactory.create_filesystem(
                read_bytes_func=self.disk_manager.read_bytes,
                write_bytes_func=self.disk_manager.write_bytes,
                flush_func=self.disk_manager.flush
            )
            bpb = self.fs.get_bpb_info()
            self.disk_manager.sectors_per_track = bpb['sectors_per_track']
            self.disk_manager.num_heads = bpb['num_heads']
            self.disk_manager.sector_size = bpb['bytes_per_sector']
            self.disk_manager.total_sectors = bpb['total_sectors']
            self.disk_manager.num_cylinders = self.disk_manager.total_sectors // (self.disk_manager.sectors_per_track * self.disk_manager.num_heads)
            self.root_node = self.build_fs_tree()
            self.current_node = self.root_node
            self.current_path = "/"
            self.refresh_filesystem_ui()
            if self.disk_manager.num_heads > 1:
                self.head_action.setEnabled(True)
                self.head_action.setText(f"Switch to Head {1 - self.current_head}")
            else:
                self.head_action.setEnabled(False)
                self.head_action.setText("Single-sided disk")
            self.statusBar().showMessage(f"Loaded physical floppy using {format_name}")
        except Exception as e:
            self.reset_ui()
            QMessageBox.critical(self, "Error", f"Failed to open physical floppy: {str(e)}")

    def update_bpb_info(self):
        if self.fs is None:
            self.bpb_info.setText("No disk image loaded")
            return
        bpb = self.fs.get_bpb_info()
        cluster_size = bpb['sectors_per_cluster'] * bpb['bytes_per_sector']
        free_bytes = self.free_space * cluster_size
        total_bytes = self.total_space * cluster_size
        free_kb = free_bytes / 1024
        total_kb = total_bytes / 1024
        percent_free = (free_bytes / total_bytes * 100) if total_bytes > 0 else 0
        sectors_per_cylinder = bpb['sectors_per_track'] * bpb['num_heads']
        num_tracks = math.ceil(bpb['total_sectors'] / sectors_per_cylinder)
        info = (
            f"Bytes per Sector: {bpb['bytes_per_sector']}\n"
            f"Sectors per Cluster: {bpb['sectors_per_cluster']}\n"
            f"Reserved Sectors: {bpb['reserved_sectors']}\n"
            f"Number of FATs: {bpb['num_fats']}\n"
            f"Root Entries: {bpb['root_entries']}\n"
            f"Total Sectors: {bpb['total_sectors']}\n"
            f"Media Descriptor: 0x{bpb['media_descriptor']:02X}\n"
            f"Sectors per FAT: {bpb['sectors_per_fat']}\n"
            f"Sectors per Track: {bpb['sectors_per_track']}\n"
            f"Number of Heads: {bpb['num_heads']}\n"
            f"Number of Tracks: {num_tracks}\n"
            f"Hidden Sectors: {bpb['hidden_sectors']}\n"
            f"Disk Type: {self.fs.get_disk_type()}\n"
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
        if not selected_items or not self.fs:
            return

        item = selected_items[0]
        node = item.node

        if node.is_dir:
            QMessageBox.information(self, "Info", "Cannot extract directories")
            return

        try:
            file_path = self.build_full_path(node.name)
            file_data = self.perform_with_progress(
                lambda cb: self.fs.extract_file(file_path, progress_callback=cb),
                title="Extracting File..."
            )
            save_path, _ = QFileDialog.getSaveFileName(self, "Save File", node.name)
            if save_path:
                with open(save_path, 'wb') as f:
                    f.write(file_data)
                self.statusBar().showMessage(f"Extracted {node.name} to {save_path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to extract file: {str(e)}")

    def delete_selected_item(self):
        """Delete selected file or directory"""
        selected_items = self.file_list.selectedItems()
        if not selected_items or not self.fs:
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
                # self.fs.delete_item(item_path)
                self.perform_with_progress(lambda cb: self.fs.delete_item(item_path, progress_callback=cb), title="Deleting Item...")


                # Remember the current path
                current_path = self.current_path

                # Update UI
                self.refresh_filesystem_ui(current_path)

                self.statusBar().showMessage(f"Deleted {node.name}")
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
        if not self.fs or not self.current_node:
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
            # self.fs.create_directory(current_path, dir_name, datetime.datetime.now())
            self.perform_with_progress(lambda cb: self.fs.create_directory(current_path, dir_name, datetime.datetime.now(), progress_callback=cb), title="Creating Directory...")

            # Update UI
            self.refresh_filesystem_ui(current_path)

            self.statusBar().showMessage(f"Created directory {dir_name} in {current_path}")
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
        if not self.fs or not self.current_node:
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
        self.perform_with_progress(lambda cb: self.fs.insert_file(dest_path, dest_name, file_data, datetime.datetime.now(), progress_callback=cb), title="Adding File...")

        # Update UI
        self.refresh_filesystem_ui(dest_path)

        self.statusBar().showMessage(f"Added file {dest_name} to {dest_path}")

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
        if self.disk_manager and self.disk_manager.num_heads > 1:
            self.current_head = 1 - self.current_head
            self.head_action.setText(f"Switch to Head {1 - self.current_head}")
            self.draw_disk_map()
        else:
            QMessageBox.information(self, "Info", "Head switching is not available for this disk.")

    def get_busy_clusters(self):
        """Identify busy clusters and calculate free space"""
        if not self.fs:
            self.busy_clusters = []
            self.free_space = 0
            self.total_space = 0
            return

        try:
            busy_clusters = []
            free_clusters = 0
            total_clusters = 0

            # Get parameters
            if not hasattr(self.fs, 'fat_start') or not hasattr(self.fs, 'num_clusters'):
                print("Warning: Missing filesystem parameters for cluster detection")
                self.busy_clusters = []
                self.free_space = 0
                self.total_space = 0
                return

            # Read the entire first FAT using disk_manager
            fat_size_bytes = self.fs.sectors_per_fat * self.fs.sector_size
            fat_data = self.perform_with_progress(
                lambda cb: self.disk_manager.read_bytes(self.fs.fat_start, fat_size_bytes, progress_callback=cb),
                title="Reading FAT..."
            )

            # Process each cluster entry
            # Skip first two entries which are reserved
            for cluster in range(2, self.fs.num_clusters + 2):
                total_clusters += 1

                # Calculate offset into FAT
                fat_offset = int(cluster * 1.5)
                if fat_offset + 1 >= len(fat_data):
                    # This shouldn't happen with a properly initialized FAT
                    continue

                # Extract 12-bit FAT entry value
                if cluster % 2 == 0:
                    # Even cluster: uses the low 12 bits
                    value = fat_data[fat_offset] | ((fat_data[fat_offset + 1] & 0x0F) << 8)
                else:
                    # Odd cluster: uses the high 12 bits
                    value = ((fat_data[fat_offset] >> 4) | (fat_data[fat_offset + 1] << 4)) & 0xFFF

                if value == 0:
                    free_clusters += 1
                else:
                    busy_clusters.append(cluster)

            self.busy_clusters = busy_clusters
            self.free_space = free_clusters
            self.total_space = total_clusters

        except Exception as e:
            self.busy_clusters = []
            self.free_space = 0
            self.total_space = 0
            print(f"Error getting busy clusters: {e}")
            import traceback
            traceback.print_exc()

    def draw_disk_map(self):
        """Draw a visual representation of the disk's layout"""
        self.disk_map_scene.clear()

        if not self.fs:
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
            if self.current_head >= self.disk_manager.num_heads:
                self.disk_map_scene.addText("No data for this head").setPos(10, 10)
                return

            view_width = self.disk_map_view.width()
            view_height = self.disk_map_view.height()
            x0 = view_width / 2
            y0 = view_height / 2
            r_min = min(view_width, view_height) * 0.1
            r_max = min(view_width, view_height) * 0.45

            # Get disk geometry from disk_manager
            disk_manager = self.disk_manager
            sectors_per_track = disk_manager.sectors_per_track
            total_sectors = disk_manager.total_sectors
            num_heads = disk_manager.num_heads
            sector_size = disk_manager.sector_size

            # Get BPB parameters
            bpb = self.fs.get_bpb_info()
            bytes_per_sector = bpb['bytes_per_sector']
            sectors_per_cluster = bpb['sectors_per_cluster']
            reserved = bpb['reserved_sectors']
            num_fats = bpb['num_fats']
            fat_size = bpb['sectors_per_fat']
            root_entries = bpb['root_entries']

            # Calculate root directory sectors
            root_dir_sectors = math.ceil((root_entries * 32) / bytes_per_sector)

            # Calculate first data sector
            first_data_sector = reserved + (num_fats * fat_size) + root_dir_sectors

            # Calculate how many cylinders we have
            sectors_per_cylinder = sectors_per_track * num_heads
            num_cylinders = math.ceil(total_sectors / sectors_per_cylinder)

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
                cyl_start_sector = c * sectors_per_cylinder + (self.current_head * sectors_per_track)

                for i in range(sectors_per_track):
                    s = cyl_start_sector + i

                    if s < total_sectors:
                        color = self.get_sector_color(s, sectors_per_cluster, reserved, fat_size, root_dir_sectors, first_data_sector)

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
