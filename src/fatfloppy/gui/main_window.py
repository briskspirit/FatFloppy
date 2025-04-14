# src/fatfloppy/gui/main_window.py
import os
import datetime

from PyQt6.QtCore import (QCoreApplication, Qt)
from PyQt6.QtGui import (QAction, QFont)
from PyQt6.QtWidgets import (QDockWidget, QFileDialog, QInputDialog, QLabel,
                             QMainWindow, QMessageBox, QProgressDialog, QToolBar,
                             QTreeWidget, QTreeWidgetItem, QHeaderView, QAbstractItemView)

from ..core.controller import DiskController
from .dialogs import DriveSelectionDialog
from .disk_map import DiskMapView
from .file_browser import DragDropTreeWidget
from .models import FileSystemNode
from .operations import OperationWorker

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

        # Initialize the UI
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
        self.setWindowTitle("FatFloppy Disk Browser")
        self.setGeometry(100, 100, 1200, 800)

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
        self.disk_map = DiskMapView(self)
        self.disk_map_view = self.disk_map.view
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
        self.disk_map.scene.clear()
        self.disk_map.scene.addText("No disk image loaded").setPos(10, 10)

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
            # Create and show the drive selection dialog
            dialog = DriveSelectionDialog(self)
            if not dialog.exec():
                return  # User cancelled

            drive_letter, drive_size, format_info = dialog.get_selection()

            self.controller = DiskController()
            if self.controller.open_disk(None, "physical", drive_letter=drive_letter, drive_size=drive_size, format_info=format_info):
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

                format_name = self.controller.detect_format()
                format_text = f" using {format_name}" if format_name else ""

                # Add format info if custom was selected
                if format_info and not format_info.get("profile_name"):
                    format_text += f" (Custom format: {format_info.get('cylinders')}x{format_info.get('heads')}x{format_info.get('sectors_per_track')})"

                self.statusBar().showMessage(f"Loaded physical floppy{format_text} (Drive: {drive_letter}, Size: {drive_size}\")")
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
        self.disk_map.draw_disk_map(
            self.controller,
            self.current_head,
            self.busy_clusters,
            self.free_space,
            self.total_space,
            self.app_font
        )

def run_gui():
    import sys
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QIcon
    import os

    app = QApplication(sys.argv)
    app.setApplicationName("FatFloppy")

    current_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = current_dir
    while not os.path.exists(os.path.join(root_dir, 'assets')) and root_dir != os.path.dirname(root_dir):
        root_dir = os.path.dirname(root_dir)
    icon_path = os.path.join(root_dir, 'assets', 'icons', 'fatfloppy_icon.png')

    if os.path.exists(icon_path):
        app_icon = QIcon(icon_path)
        app.setWindowIcon(app_icon)

    window = FileBrowserApp()
    window.show()
    sys.exit(app.exec())
