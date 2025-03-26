import datetime
import math
import os
import tempfile

from PyQt6.QtCore import QPointF, Qt, QMimeData, QUrl, QTimer
from PyQt6.QtGui import QAction, QBrush, QFont, QPainter, QPen, QPolygonF, QIcon, QDrag
from PyQt6.QtWidgets import (QDockWidget, QFileDialog, QGraphicsEllipseItem,
                             QGraphicsLineItem, QGraphicsPolygonItem,
                             QGraphicsScene, QGraphicsView, QInputDialog,
                             QLabel, QMainWindow, QMessageBox, QToolBar,
                             QTreeWidget, QTreeWidgetItem, QAbstractItemView)

from diskmanager import FloppyDiskManager, ImageFileManager
from fat import FAT12FileSystem
from floppybpb import FloppyBPB


class ResizableGraphicsView(QGraphicsView):
    def __init__(self, scene, app, parent=None):
        super().__init__(scene, parent)
        self.app = app
        self.setSceneRect(0, 0, self.width(), self.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setSceneRect(0, 0, self.width(), self.height())
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
        """Process the files dropped onto the widget"""
        if not hasattr(self.parent, 'fs') or not self.parent.current_node:
            QMessageBox.warning(self.parent, "Warning", "No disk image loaded")
            return

        # Get current path
        current_path = self.parent.current_path

        for file_path in file_paths:
            # Skip directories for now - we could handle them recursively in the future
            if os.path.isdir(file_path):
                QMessageBox.information(self.parent, "Info",
                                       f"Directory dropping is not yet supported: {os.path.basename(file_path)}")
                continue

            # Get destination filename (8.3 format)
            base_name = os.path.basename(file_path)
            # Trim to 8.3 format if needed
            if len(base_name) > 12 or base_name.count('.') > 1:
                parts = base_name.split('.')
                if len(parts) > 1:
                    base_name = parts[0][:8] + '.' + parts[-1][:3]
                else:
                    base_name = parts[0][:8]

            # Ask user to confirm or modify filename
            new_name, ok = QInputDialog.getText(self.parent, "File Name",
                                              f"Enter file name for {base_name} (8.3 format):",
                                              text=base_name)
            if not ok or not new_name:
                continue

            try:
                # Read file data
                with open(file_path, 'rb') as f:
                    file_data = f.read()

                # Add file to disk
                self.parent.fs.insert_file(current_path, new_name, file_data, datetime.datetime.now())

                # Update UI
                self.parent.root_node = self.parent.build_fs_tree()
                self.parent.populate_tree()

                # Restore the current directory selection
                self.parent.navigate_to_path(current_path)

                # Update busy clusters and disk map
                self.parent.get_busy_clusters()
                self.parent.draw_disk_map()

                self.parent.statusBar().showMessage(f"Added file {new_name} to {current_path}")
            except Exception as e:
                QMessageBox.critical(self.parent, "Error", f"Failed to add file: {str(e)}")

    def mouseMoveEvent(self, event):
        """Handle dragging files out of the application"""
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return

        # Get selected items
        items = self.selectedItems()
        if not items:
            return

        # Only allow dragging files, not directories
        files_to_drag = [item for item in items if not item.node.is_dir]
        if not files_to_drag:
            return

        # Create drag object
        drag = QDrag(self)
        mime_data = QMimeData()

        # Create temporary directory for storing files
        import tempfile
        temp_dir = tempfile.mkdtemp()
        urls = []

        for item in files_to_drag:
            node = item.node

            try:
                # Build the full path to the file
                file_path = self.parent.current_path
                if file_path != "/":
                    file_path += "/"
                file_path += node.name

                # Extract the file data
                file_data = self.parent.fs.extract_file(file_path)

                # Create a temporary file with the original filename
                temp_path = os.path.join(temp_dir, node.name)

                # Write data to temp file
                with open(temp_path, 'wb') as f:
                    f.write(file_data)

                # Add URL for drag operation
                urls.append(QUrl.fromLocalFile(temp_path))

            except Exception as e:
                QMessageBox.critical(self.parent, "Error", f"Failed to prepare file for dragging: {str(e)}")

        # Set URLs for dragging
        if urls:
            mime_data.setUrls(urls)
            drag.setMimeData(mime_data)

            # Execute drag operation
            result = drag.exec(Qt.DropAction.CopyAction)

            # Clean up temp directory after a delay to allow the target application to read the files
            def cleanup_temp_files():
                try:
                    import shutil
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except:
                    pass

            # Use QTimer to delay cleanup
            QTimer.singleShot(10000, cleanup_temp_files)  # 10-second delay

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

class FileBrowserApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.root_node = self.create_dummy_fs()
        self.current_node = self.root_node
        self.current_path = "/"
        self.current_head = 0
        self.busy_clusters = []
        self.initUI()

        # Set up application-wide monospaced font
        self.setup_fonts()

    def setup_fonts(self):
        """Setup application-wide monospaced font with fallbacks."""
        # List of monospaced fonts in order of preference
        # These fonts are commonly available across different platforms
        monospace_fonts = [
            "monospace",          # Generic fallback
            "Courier New",        # All platforms
            "DejaVu Sans Mono",   # Linux
            "Consolas",           # Windows
            "Menlo",              # macOS
            "Liberation Mono",    # Linux
            "Monaco",             # macOS
        ]

        # Create font with the first available font in the list
        app_font = QFont()
        app_font.setFamily(monospace_fonts[0])  # Start with first preference
        app_font.setStyleHint(QFont.StyleHint.Monospace)  # Hint to use monospace if first choice unavailable
        app_font.setFixedPitch(True)  # Ensure fixed pitch
        app_font.setPointSize(10)     # Set reasonable size

        # Set font for the entire application
        self.setFont(app_font)

        # Apply the font to specific widgets that might need explicit setting
        self.tree_widget.setFont(app_font)
        self.file_list.setFont(app_font)
        self.bpb_info.setFont(app_font)

        # Create a slightly larger font for headings and labels
        header_font = QFont(app_font)
        header_font.setPointSize(11)
        header_font.setBold(True)

        # Apply to headers
        self.tree_widget.headerItem().setFont(0, header_font)
        for i in range(self.file_list.columnCount()):
            self.file_list.headerItem().setFont(i, header_font)

    def create_dummy_fs(self):
        root_node = FileSystemNode("Root", is_dir=True, modified="N/A", attributes="-")
        dir1 = FileSystemNode("DIR1", is_dir=True, modified="2023-01-01", attributes="-", parent=root_node)
        file1 = FileSystemNode("FILE1.TXT", size=1024, modified="2023-01-01", attributes="-", parent=dir1)
        dir2 = FileSystemNode("DIR2", is_dir=True, modified="2023-01-01", attributes="-", parent=root_node)
        file2 = FileSystemNode("FILE2.BIN", size=2048, modified="2023-01-02", attributes="-", parent=dir2)
        root_node.appendChild(dir1)
        root_node.appendChild(dir2)
        dir1.appendChild(file1)
        dir2.appendChild(file2)
        return root_node

    def build_fs_tree(self):
        if not hasattr(self, 'fs') or self.fs is None:
            return self.create_dummy_fs()

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
        self.tree_widget.setHeaderLabel("Directory Tree")
        self.populate_tree()
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
        self.file_list.setHeaderLabels(["Name", "Size", "Date/Time", "Attributes"])
        self.file_list.setDragEnabled(True)
        self.file_list.setAcceptDrops(True)
        self.file_list.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.update_file_list()
        self.file_list_dock.setWidget(self.file_list)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.file_list_dock)

        # Disk Map Dock
        self.disk_map_dock = QDockWidget("Disk Map", self)
        self.disk_map_scene = QGraphicsScene()
        self.disk_map_view = ResizableGraphicsView(self.disk_map_scene, self)
        self.disk_map_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.draw_disk_map()
        self.disk_map_dock.setWidget(self.disk_map_view)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.disk_map_dock)

        self.splitDockWidget(self.file_list_dock, self.disk_map_dock, Qt.Orientation.Horizontal)
        self.resizeDocks([self.file_list_dock, self.disk_map_dock], [480, 720], Qt.Orientation.Horizontal)

        self.statusBar().showMessage("Ready")

    def open_disk_image_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Open Disk Image", "", "Disk Images (*.ima *.img)")
        if not file_path:
            return

        try:
            # Create a disk manager for the image file
            disk_manager = ImageFileManager(file_path)

            # Create BPB instance to parse boot sector
            bpb = FloppyBPB(disk_manager)

            # Get FAT12 parameters
            fat_params = bpb.get_fat12_params()

            # Create FAT12 filesystem instance
            self.fs = FAT12FileSystem(disk_manager, fat_params)

            # Set disk geometry based on BPB
            disk_manager.sectors_per_track = bpb.sectors_per_track
            disk_manager.num_heads = bpb.num_heads
            disk_manager.sector_size = bpb.bytes_per_sector
            disk_manager.total_sectors = bpb.total_sectors

            # Update UI components
            self.root_node = self.build_fs_tree()
            self.current_node = self.root_node
            self.current_path = "/"
            self.update_bpb_info(bpb)
            self.populate_tree()
            self.update_file_list()
            self.get_busy_clusters()
            self.draw_disk_map()

            self.statusBar().showMessage(f"Loaded: {file_path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to open disk image: {str(e)}")

    def open_physical_floppy(self):
        try:
            # Ask for device name
            device_name, ok = QInputDialog.getText(self, "Device Selection",
                                                  "Enter device name (e.g., COM3):")
            if not ok or not device_name:
                device_name = None

            format_name, ok = QInputDialog.getText(self, "Format Selection",
                                                  "Enter format name (e.g., ibm.1440, ibm.scan):",
                                                  text="ibm.scan")
            if not ok or not format_name:
                format_name = "ibm.scan"

            # Create a FloppyDiskManager for the physical floppy
            disk_manager = FloppyDiskManager(device_name, format_name=format_name)

            # Create BPB instance to parse boot sector
            bpb = FloppyBPB(disk_manager)

            # Get FAT12 parameters
            fat_params = bpb.get_fat12_params()

            # Create FAT12 filesystem instance
            self.fs = FAT12FileSystem(disk_manager, fat_params)

            # Update UI components
            self.root_node = self.build_fs_tree()
            self.current_node = self.root_node
            self.current_path = "/"
            self.update_bpb_info(bpb)
            self.populate_tree()
            self.update_file_list()
            self.get_busy_clusters()
            self.draw_disk_map()

            self.statusBar().showMessage(f"Loaded physical floppy using {format_name}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to open physical floppy: {str(e)}")

    def update_bpb_info(self, bpb=None):
        if bpb is None and not hasattr(self, 'fs'):
            self.bpb_info.setText("No disk image loaded")
            return

        if bpb is None:
            # Try to get BPB from the disk manager
            try:
                bpb = FloppyBPB(self.fs.disk_manager)
            except:
                self.bpb_info.setText("Error reading boot sector")
                return

        info = (
            f"Bytes per Sector: {bpb.bytes_per_sector}\n"
            f"Sectors per Cluster: {bpb.sectors_per_cluster}\n"
            f"Reserved Sectors: {bpb.reserved_sectors}\n"
            f"Number of FATs: {bpb.num_fats}\n"
            f"Root Entries: {bpb.root_entries}\n"
            f"Total Sectors: {bpb.total_sectors}\n"
            f"Media Descriptor: 0x{bpb.media_descriptor:02X}\n"
            f"Sectors per FAT: {bpb.sectors_per_fat}\n"
            f"Sectors per Track: {bpb.sectors_per_track}\n"
            f"Number of Heads: {bpb.num_heads}\n"
            f"Hidden Sectors: {bpb.hidden_sectors}\n"
            f"Total Size: {bpb.total_sectors * bpb.bytes_per_sector} bytes\n"
            f"Disk Type: {bpb.get_disk_type()}"
        )
        self.bpb_info.setText(info)

    def get_current_path(self):
        """Build the full path to the current directory."""
        if hasattr(self, 'current_path'):
            return self.current_path

        path_parts = []
        curr = self.current_node
        while curr and curr.parent:  # Don't include "Root" in the path
            path_parts.insert(0, curr.name)
            curr = curr.parent
        return "/" + "/".join(path_parts) if path_parts else "/"

    def populate_tree(self):
        self.tree_widget.clear()
        if not hasattr(self, 'root_node'):
            return
        root_item = QTreeWidgetItem(self.tree_widget, ["Root"])
        root_item.node = self.root_node
        self._populate_tree(self.root_node, root_item)
        self.tree_widget.expandAll()

    def _populate_tree(self, node, parent_item):
        for child in node.children:
            if child.is_dir:
                child_item = QTreeWidgetItem(parent_item, [child.name])
                child_item.node = child
                self._populate_tree(child, child_item)

    def select_directory(self, item):
        self.current_node = item.node

        # Update current_path
        path_parts = []
        curr = self.current_node
        while curr and curr.parent:  # Don't include "Root" in the path
            path_parts.insert(0, curr.name)
            curr = curr.parent
        self.current_path = "/" + "/".join(path_parts) if path_parts else "/"

        self.update_file_list()
        self.statusBar().showMessage(f"Viewing: {self.current_path}")

    def update_file_list(self):
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
        if not selected_items or not hasattr(self, 'fs'):
            return

        item = selected_items[0]
        node = item.node

        if node.is_dir:
            QMessageBox.information(self, "Info", "Cannot extract directories")
            return

        try:
            # Build the full path to the file
            file_path = self.current_path
            if file_path != "/":
                file_path += "/"
            file_path += node.name

            # Extract the file
            file_data = self.fs.extract_file(file_path)

            # Save to local filesystem
            save_path, _ = QFileDialog.getSaveFileName(self, "Save File", node.name)
            if save_path:
                with open(save_path, 'wb') as f:
                    f.write(file_data)
                self.statusBar().showMessage(f"Extracted {node.name} to {save_path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to extract file: {str(e)}")

    def delete_selected_item(self):
        selected_items = self.file_list.selectedItems()
        if not selected_items or not hasattr(self, 'fs'):
            return

        item = selected_items[0]
        node = item.node

        try:
            # Build the full path to the file/directory
            item_path = self.current_path
            if item_path != "/":
                item_path += "/"
            item_path += node.name

            # Confirm deletion
            msg_type = "directory" if node.is_dir else "file"
            if QMessageBox.question(self, "Confirm Deletion",
                                  f"Are you sure you want to delete the {msg_type} {node.name}?",
                                  QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                # Delete the item
                self.fs.delete_item(item_path)

                # Remember the current path
                current_path = self.current_path

                # Update UI
                self.root_node = self.build_fs_tree()
                self.populate_tree()

                # Restore the current directory selection
                self.navigate_to_path(current_path)

                # Update busy clusters and disk map
                self.get_busy_clusters()
                self.draw_disk_map()

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
            self.tree_widget.setCurrentItem(self.tree_widget.topLevelItem(0))
            return

        parts = path.strip("/").split("/")
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
        if not hasattr(self, 'fs') or not self.current_node:
            return

        # Get current path
        current_path = self.current_path

        # Get new directory name from user
        dir_name, ok = QInputDialog.getText(self, "Create New Directory",
                                          "Enter directory name (8.3 format):")
        if not ok or not dir_name:
            return

        try:
            self.fs.create_directory(current_path, dir_name, datetime.datetime.now())

            # Update UI
            self.root_node = self.build_fs_tree()
            self.populate_tree()

            # Restore the current directory selection
            self.navigate_to_path(current_path)

            # Update busy clusters and disk map
            self.get_busy_clusters()
            self.draw_disk_map()

            self.statusBar().showMessage(f"Created directory {dir_name} in {current_path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to create directory: {str(e)}")

    def add_file(self):
        if not hasattr(self, 'fs') or not self.current_node:
            return

        # Get current path
        current_path = self.current_path

        # Get file to add
        file_path, _ = QFileDialog.getOpenFileName(self, "Select File to Add")
        if not file_path:
            return

        # Get destination filename (8.3 format)
        base_name = os.path.basename(file_path)
        # Trim to 8.3 format if needed
        if len(base_name) > 12 or base_name.count('.') > 1:
            parts = base_name.split('.')
            if len(parts) > 1:
                base_name = parts[0][:8] + '.' + parts[-1][:3]
            else:
                base_name = parts[0][:8]

        new_name, ok = QInputDialog.getText(self, "File Name",
                                          "Enter file name (8.3 format):",
                                          text=base_name)
        if not ok or not new_name:
            return

        try:
            # Read file data
            with open(file_path, 'rb') as f:
                file_data = f.read()

            # Add file to disk
            self.fs.insert_file(current_path, new_name, file_data, datetime.datetime.now())

            # Update UI
            self.root_node = self.build_fs_tree()
            self.populate_tree()

            # Restore the current directory selection
            self.navigate_to_path(current_path)

            # Update busy clusters and disk map
            self.get_busy_clusters()
            self.draw_disk_map()

            self.statusBar().showMessage(f"Added file {new_name} to {current_path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to add file: {str(e)}")

    def generate_arc_points(self, x0, y0, radius, theta_start, theta_end, num_points):
        points = []
        step = (theta_end - theta_start) / (num_points - 1)
        for i in range(num_points):
            theta = theta_start + i * step
            x = x0 + radius * math.cos(theta)
            y = y0 + radius * math.sin(theta)
            points.append(QPointF(x, y))
        return points

    def toggle_head(self):
        self.current_head = 1 - self.current_head
        self.head_action.setText(f"Switch to Head {1 - self.current_head}")
        self.draw_disk_map()

    def get_busy_clusters(self):
        if not hasattr(self, 'fs'):
            self.busy_clusters = []
            return

        try:
            busy_clusters = []
            free_clusters = 0
            total_clusters = 0

            # Get important parameters
            if not hasattr(self.fs, 'fat_start') or not hasattr(self.fs, 'num_clusters'):
                print("Warning: Missing filesystem parameters for cluster detection")
                self.busy_clusters = []
                return

            # Read the entire first FAT
            fat_size_bytes = int(self.fs.num_clusters * 1.5) + 3  # Add a buffer for the first entries
            fat_data = self.fs.disk_manager.read_bytes(self.fs.fat_start, fat_size_bytes)

            # First two entries are special
            # Start checking from cluster 2 (first data cluster)
            for cluster in range(2, self.fs.num_clusters + 2):
                total_clusters += 1

                # Calculate offset and extract value
                fat_offset = int(cluster * 1.5)
                if cluster % 2 == 0:
                    # Even cluster: uses the low 12 bits
                    if fat_offset + 1 < len(fat_data):
                        value = fat_data[fat_offset] | ((fat_data[fat_offset + 1] & 0x0F) << 8)
                    else:
                        value = 0  # Default if we can't read
                else:
                    # Odd cluster: uses the high 12 bits
                    if fat_offset < len(fat_data):
                        value = (fat_data[fat_offset] >> 4) | (fat_data[fat_offset - 1] << 4)
                    else:
                        value = 0  # Default if we can't read

                if value == 0:
                    free_clusters += 1
                else:
                    busy_clusters.append(cluster)

            self.busy_clusters = busy_clusters
            print(f"Found {len(busy_clusters)} busy clusters, {free_clusters} free clusters out of {total_clusters} total")

        except Exception as e:
            self.busy_clusters = []
            print(f"Error getting busy clusters: {e}")
            import traceback
            traceback.print_exc()  # Print full stack trace for debugging

    def draw_disk_map(self):
        self.disk_map_scene.clear()

        if not hasattr(self, 'fs'):
            self.disk_map_scene.addText("No disk image loaded").setPos(10, 10)
            return

        try:
            view_width = self.disk_map_view.width()
            view_height = self.disk_map_view.height()
            x0 = view_width / 2
            y0 = view_height / 2
            r_min = min(view_width, view_height) * 0.1
            r_max = min(view_width, view_height) * 0.45

            # Get disk geometry from filesystem
            disk_manager = self.fs.disk_manager
            sectors_per_track = getattr(disk_manager, 'sectors_per_track', None)
            total_sectors = getattr(disk_manager, 'total_sectors', None)
            num_heads = getattr(disk_manager, 'num_heads', None)

            # Check if geometry info is available
            if not sectors_per_track or not total_sectors or not num_heads or sectors_per_track <= 0 or total_sectors <= 0 or num_heads <= 0:
                self.disk_map_scene.addText("Disk geometry not available (using default values)").setPos(10, 10)
                # Use default values for 1.44MB floppy if geometry is missing
                sectors_per_track = 18
                total_sectors = 2880
                num_heads = 2

            num_cylinders = total_sectors // (sectors_per_track * num_heads)
            angle_per_sector = 360 / sectors_per_track
            num_points = 20

            # Draw legend
            legend_x = view_width - 150
            legend_y = 10
            colors = [
                ("Boot Sector", Qt.GlobalColor.red),
                ("FAT1", Qt.GlobalColor.green),
                ("FAT2", Qt.GlobalColor.blue),
                ("Root Directory", Qt.GlobalColor.yellow),
                ("Busy Sector", Qt.GlobalColor.magenta),
                ("Free Sector", Qt.GlobalColor.gray),
            ]
            for i, (label, color) in enumerate(colors):
                rect = QGraphicsPolygonItem(QPolygonF([
                    QPointF(legend_x, legend_y + i * 15),
                    QPointF(legend_x + 10, legend_y + i * 15),
                    QPointF(legend_x + 10, legend_y + i * 15 + 10),
                    QPointF(legend_x, legend_y + i * 15 + 10)
                ]))
                rect.setBrush(QBrush(color))
                self.disk_map_scene.addItem(rect)
                text = self.disk_map_scene.addText(label)
                text.setPos(legend_x + 15, legend_y + i * 15)
                text.setFont(QFont("Arial", 12))

            # Draw sectors
            for c in range(num_cylinders):
                for i in range(sectors_per_track):
                    s = (c * num_heads * sectors_per_track) + (self.current_head * sectors_per_track) + i
                    if s < total_sectors:
                        color = self.get_sector_color(s)
                        theta_start = math.radians(i * angle_per_sector)
                        theta_end = math.radians((i + 1) * angle_per_sector)
                        r_outer = r_max - (r_max - r_min) * c / num_cylinders
                        r_inner = r_max - (r_max - r_min) * (c + 1) / num_cylinders
                        inner_points = self.generate_arc_points(x0, y0, r_inner, theta_start, theta_end, num_points)
                        outer_points = self.generate_arc_points(x0, y0, r_outer, theta_end, theta_start, num_points)
                        points = inner_points + outer_points
                        polygon = QGraphicsPolygonItem(QPolygonF(points))
                        polygon.setBrush(QBrush(color))
                        self.disk_map_scene.addItem(polygon)

            # Draw radial lines (sector boundaries)
            for sector in range(sectors_per_track):
                theta = math.radians(sector * angle_per_sector)
                p1 = QPointF(x0 + r_min * math.cos(theta), y0 + r_min * math.sin(theta))
                p2 = QPointF(x0 + r_max * math.cos(theta), y0 + r_max * math.sin(theta))
                line = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
                line.setPen(QPen(Qt.GlobalColor.black, 1))
                self.disk_map_scene.addItem(line)

            # Draw concentric circles (cylinder boundaries)
            for cylinder in range(1, num_cylinders):
                r = r_max - (r_max - r_min) * cylinder / num_cylinders
                ellipse = QGraphicsEllipseItem(x0 - r, y0 - r, 2 * r, 2 * r)
                ellipse.setPen(QPen(Qt.GlobalColor.black, 1))
                ellipse.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                self.disk_map_scene.addItem(ellipse)

            # Add head indicator
            head_text = self.disk_map_scene.addText(f"Head {self.current_head}")
            head_text.setPos(10, 10)
            head_text.setFont(QFont("Arial", 12))

        except Exception as e:
            self.disk_map_scene.addText(f"Error drawing disk map: {str(e)}").setPos(10, 10)
            import traceback
            traceback.print_exc()  # Print full stack trace for debugging

    def get_sector_color(self, s):
        """Determine the color for a sector based on its role in FAT12 filesystem."""
        if not hasattr(self, 'fs'):
            return Qt.GlobalColor.gray

        try:
            # Get parameters with fallbacks
            reserved = getattr(self.fs, 'reserved_sectors', 1)
            fat_size = getattr(self.fs, 'sectors_per_fat', 9)
            root_size = getattr(self.fs, 'root_dir_sectors', 14)
            sectors_per_cluster = getattr(self.fs.params, 'sectors_per_cluster', 1) if hasattr(self.fs, 'params') else 1

            # Ensure we have valid values
            if reserved is None or reserved <= 0: reserved = 1
            if fat_size is None or fat_size <= 0: fat_size = 9
            if root_size is None or root_size <= 0: root_size = 14
            if sectors_per_cluster is None or sectors_per_cluster <= 0: sectors_per_cluster = 1

            # Determine sector type based on sector number
            if s < reserved:
                return Qt.GlobalColor.red  # Boot sector and reserved
            elif reserved <= s < reserved + fat_size:
                return Qt.GlobalColor.green  # FAT1
            elif reserved + fat_size <= s < reserved + 2 * fat_size:
                return Qt.GlobalColor.blue  # FAT2
            elif reserved + 2 * fat_size <= s < reserved + 2 * fat_size + root_size:
                return Qt.GlobalColor.yellow  # Root directory
            else:
                # Data area
                try:
                    # Calculate cluster number from sector
                    first_data_sector = reserved + 2 * fat_size + root_size
                    data_sector = s - first_data_sector
                    cluster = (data_sector // sectors_per_cluster) + 2  # First data cluster is 2

                    # Check if this cluster is in the busy list
                    if self.busy_clusters and cluster in self.busy_clusters:
                        return Qt.GlobalColor.magenta  # Busy cluster
                    else:
                        return Qt.GlobalColor.gray  # Free cluster
                except Exception as e:
                    print(f"Error determining cluster for sector {s}: {e}")
                    return Qt.GlobalColor.lightGray
        except Exception as e:
            print(f"Error determining sector color: {e}")
            return Qt.GlobalColor.lightGray  # Default for any errors
