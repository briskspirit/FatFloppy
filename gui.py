import sys
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTreeWidget, QTreeWidgetItem,
    QLabel, QGraphicsScene, QGraphicsView, QGraphicsPolygonItem,
    QGraphicsLineItem, QGraphicsEllipseItem, QDockWidget, QFileDialog,
    QVBoxLayout, QWidget, QPushButton, QMessageBox, QInputDialog
)
from PyQt6.QtGui import QPolygonF, QBrush, QPen, QPainter, QFont, QAction
from PyQt6.QtCore import Qt, QPointF
import math
import struct
import datetime
import os
from fat import FAT12FileSystem
from diskmanager import ImageFileManager, FloppyDiskManager, MemoryDiskManager
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
        self.current_head = 0
        self.busy_clusters = []
        self.initUI()

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

        root_node = FileSystemNode("Root", is_dir=True, attributes="-")
        files = self.fs.list_files()
        node_dict = {"/": root_node}

        for file in files:
            path = file['name']
            parts = path.strip("/").split("/")
            parent_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
            name = parts[-1]
            parent = node_dict.get(parent_path, root_node)
            node = FileSystemNode(
                name=name,
                size=file['size'],
                is_dir=file['is_dir'],
                modified=file['datetime'].strftime("%Y-%m-%d %H:%M:%S"),
                attributes=file['attributes'],
                parent=parent
            )
            parent.appendChild(node)
            if file['is_dir']:
                node_dict[file['name']] = node
        return root_node

    def initUI(self):
        self.setWindowTitle("FAT12 File Browser")
        self.setGeometry(100, 100, 1200, 800)

        # Menu Bar
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")

        open_image_action = QAction("Open Disk Image File", self)
        open_image_action.triggered.connect(self.open_disk_image_file)
        file_menu.addAction(open_image_action)

        open_floppy_action = QAction("Open Physical Floppy", self)
        open_floppy_action.triggered.connect(self.open_physical_floppy)
        file_menu.addAction(open_floppy_action)

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Toolbar
        toolbar = self.addToolBar("Head Selection")
        self.head_action = QAction("Switch to Head 1", self)
        self.head_action.triggered.connect(self.toggle_head)
        toolbar.addAction(self.head_action)

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
        self.file_list = QTreeWidget()
        self.file_list.setHeaderLabels(["Name", "Size", "Date/Time", "Attributes"])
        self.update_file_list()
        self.file_list_dock.setWidget(self.file_list)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.file_list_dock)

        # Action buttons
        action_dock = QDockWidget("Actions", self)
        action_widget = QWidget()
        action_layout = QVBoxLayout(action_widget)

        extract_button = QPushButton("Extract Selected File")
        extract_button.clicked.connect(self.extract_selected_file)
        action_layout.addWidget(extract_button)

        delete_button = QPushButton("Delete Selected Item")
        delete_button.clicked.connect(self.delete_selected_item)
        action_layout.addWidget(delete_button)

        create_dir_button = QPushButton("Create Directory")
        create_dir_button.clicked.connect(self.create_directory)
        action_layout.addWidget(create_dir_button)

        add_file_button = QPushButton("Add File")
        add_file_button.clicked.connect(self.add_file)
        action_layout.addWidget(add_file_button)

        action_dock.setWidget(action_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, action_dock)

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
        self.update_file_list()
        current_path = self.get_current_path()
        self.statusBar().showMessage(f"Viewing: {current_path}")

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
            path_parts = []
            curr = node
            while curr.parent:
                path_parts.insert(0, curr.name)
                curr = curr.parent

            file_path = "/" + "/".join(path_parts)

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
            path_parts = []
            curr = node
            while curr.parent:
                path_parts.insert(0, curr.name)
                curr = curr.parent

            item_path = "/" + "/".join(path_parts)

            # Confirm deletion
            msg_type = "directory" if node.is_dir else "file"
            if QMessageBox.question(self, "Confirm Deletion", 
                                  f"Are you sure you want to delete the {msg_type} {node.name}?",
                                  QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                # Delete the item
                self.fs.delete_item(item_path)

                # Update UI
                self.root_node = self.build_fs_tree()
                self.populate_tree()
                self.update_file_list()
                self.get_busy_clusters()
                self.draw_disk_map()

                self.statusBar().showMessage(f"Deleted {node.name}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to delete item: {str(e)}")

    def create_directory(self):
        if not hasattr(self, 'fs') or not self.current_node:
            return

        # Get current path
        current_path = self.get_current_path()

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
            self.update_file_list()
            self.get_busy_clusters()
            self.draw_disk_map()

            self.statusBar().showMessage(f"Created directory {dir_name} in {current_path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to create directory: {str(e)}")

    def add_file(self):
        if not hasattr(self, 'fs') or not self.current_node:
            return

        # Get current path
        current_path = self.get_current_path()

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
            self.update_file_list()
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
            for cluster in range(2, self.fs.num_clusters + 2):
                offset = self.fs.fat_start + int(cluster * 1.5)
                value_bytes = self.fs.disk_manager.read_bytes(offset, 2)
                value = struct.unpack('<H', value_bytes)[0]
                cluster_value = value & 0x0FFF if cluster % 2 == 0 else (value >> 4) & 0x0FFF
                if cluster_value not in [0x000, 0xFFF]:
                    busy_clusters.append(cluster)
            self.busy_clusters = busy_clusters
        except Exception as e:
            self.busy_clusters = []
            print(f"Error getting busy clusters: {e}")

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
            if not hasattr(disk_manager, 'sectors_per_track') or disk_manager.sectors_per_track is None:
                self.disk_map_scene.addText("Disk geometry not available").setPos(10, 10)
                return

            sectors_per_track = disk_manager.sectors_per_track
            total_sectors = disk_manager.total_sectors
            num_heads = disk_manager.num_heads

            if sectors_per_track == 0 or total_sectors == 0 or num_heads == 0:
                self.disk_map_scene.addText("Invalid disk geometry").setPos(10, 10)
                return

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
                ("Busy Data", Qt.GlobalColor.magenta),
                ("Free Data", Qt.GlobalColor.gray),
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

    def get_sector_color(self, s):
        """Determine the color for a sector based on its role in FAT12 filesystem."""
        if not hasattr(self, 'fs'):
            return Qt.GlobalColor.gray

        try:
            reserved = self.fs.reserved_sectors
            fat_size = self.fs.sectors_per_fat
            root_size = self.fs.root_dir_sectors

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
                cluster = (s - (reserved + 2 * fat_size + root_size)) // self.fs.sectors_per_cluster + 2
                return Qt.GlobalColor.magenta if cluster in self.busy_clusters else Qt.GlobalColor.gray
        except Exception:
            return Qt.GlobalColor.lightGray  # Default for any errors
