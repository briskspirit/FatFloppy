import sys
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTreeWidget, QTreeWidgetItem,
    QLabel, QGraphicsScene, QGraphicsView, QGraphicsPolygonItem,
    QGraphicsLineItem, QGraphicsEllipseItem, QDockWidget, QFileDialog
)
from PyQt6.QtGui import QPolygonF, QBrush, QPen, QPainter, QFont, QAction
from PyQt6.QtCore import Qt, QPointF
import math
import struct
from fat12_test import FAT12FileSystem

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
        self.attributes = attributes  # Add attributes field
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
        if not hasattr(self, 'fs'):
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
                attributes=file['attributes'],  # Pass attributes from file
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
        if file_path:
            with open(file_path, 'rb') as f:
                image_data = bytearray(f.read())
            self.fs = FAT12FileSystem(image_data)
            self.root_node = self.build_fs_tree()
            self.current_node = self.root_node
            self.update_bpb_info()
            self.populate_tree()
            self.update_file_list()
            self.draw_disk_map()
            self.statusBar().showMessage(f"Loaded: {file_path}")

    def update_bpb_info(self):
        if not hasattr(self, 'fs'):
            self.bpb_info.setText("No disk image loaded")
            return
        bpb = self.fs.bpb
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
            f"Hidden Sectors: {bpb['hidden_sectors']}\n"
            f"Total Sectors Large: {bpb['total_sectors_large']}\n"
            f"Total Size: {self.fs.total_sectors * self.fs.sector_size} bytes"
        )
        self.bpb_info.setText(info)

    def populate_tree(self):
        self.tree_widget.clear()
        if not hasattr(self, 'root_node'):
            return
        root_item = QTreeWidgetItem(self.tree_widget, ["Root"])
        root_item.node = self.root_node
        self._populate_tree(self.root_node, root_item)

    def _populate_tree(self, node, parent_item):
        for child in node.children:
            if child.is_dir:
                child_item = QTreeWidgetItem(parent_item, [child.name])
                child_item.node = child
                self._populate_tree(child, child_item)

    def select_directory(self, item):
        self.current_node = item.node
        self.update_file_list()
        self.statusBar().showMessage(f"Viewing: {self.current_node.name}")

    def update_file_list(self):
        self.file_list.clear()
        if not self.current_node:
            return
        for child in self.current_node.children:
            item = QTreeWidgetItem(self.file_list, [
                child.name,
                str(child.size) if not child.is_dir else "",
                child.modified,
                child.attributes  # Use stored attributes
            ])

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
            return []
        busy_clusters = []
        for cluster in range(2, self.fs.num_clusters + 2):
            offset = self.fs.fat_start + int(cluster * 1.5)
            value = struct.unpack('<H', self.fs.image_data[offset:offset+2])[0]
            cluster_value = value & 0x0FFF if cluster % 2 == 0 else (value >> 4) & 0x0FFF
            if cluster_value not in [0x000, 0xFFF]:
                busy_clusters.append(cluster)
        return busy_clusters

    def draw_disk_map(self):
        if not hasattr(self, 'fs'):
            self.disk_map_scene.clear()
            self.disk_map_scene.addText("No disk image loaded").setPos(10, 10)
            return
        
        view_width = self.disk_map_view.width()
        view_height = self.disk_map_view.height()
        x0 = view_width / 2
        y0 = view_height / 2
        r_min = min(view_width, view_height) * 0.1
        r_max = min(view_width, view_height) * 0.45
        num_cylinders = self.fs.bpb['total_sectors'] // (self.fs.bpb['sectors_per_track'] * self.fs.bpb['num_heads'])
        sectors_per_cylinder = self.fs.bpb['sectors_per_track']
        angle_per_sector = 360 / sectors_per_cylinder
        num_points = 20
        self.busy_clusters = self.get_busy_clusters()

        self.disk_map_scene.clear()

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

        for c in range(num_cylinders):
            for i in range(sectors_per_cylinder):
                s = (c * self.fs.bpb['num_heads'] * sectors_per_cylinder) + (self.current_head * sectors_per_cylinder) + i
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

        for sector in range(sectors_per_cylinder):
            theta = math.radians(sector * angle_per_sector)
            p1 = QPointF(x0 + r_min * math.cos(theta), y0 + r_min * math.sin(theta))
            p2 = QPointF(x0 + r_max * math.cos(theta), y0 + r_max * math.sin(theta))
            line = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
            line.setPen(QPen(Qt.GlobalColor.black, 1))
            self.disk_map_scene.addItem(line)

        for cylinder in range(1, num_cylinders):
            r = r_max - (r_max - r_min) * cylinder / num_cylinders
            ellipse = QGraphicsEllipseItem(x0 - r, y0 - r, 2 * r, 2 * r)
            ellipse.setPen(QPen(Qt.GlobalColor.black, 1))
            ellipse.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            self.disk_map_scene.addItem(ellipse)

        head_text = self.disk_map_scene.addText(f"Head {self.current_head}")
        head_text.setPos(10, 10)
        head_text.setFont(QFont("Arial", 12))

    def get_sector_color(self, s):
        if not hasattr(self, 'fs'):
            return Qt.GlobalColor.gray
        reserved = self.fs.bpb['reserved_sectors']
        fat_size = self.fs.bpb['sectors_per_fat']
        root_size = self.fs.root_dir_sectors
        if s < reserved:
            return Qt.GlobalColor.red
        elif reserved <= s < reserved + fat_size:
            return Qt.GlobalColor.green
        elif reserved + fat_size <= s < reserved + 2 * fat_size:
            return Qt.GlobalColor.blue
        elif reserved + 2 * fat_size <= s < reserved + 2 * fat_size + root_size:
            return Qt.GlobalColor.yellow
        else:
            cluster = (s - (reserved + 2 * fat_size + root_size)) // self.fs.bpb['sectors_per_cluster'] + 2
            return Qt.GlobalColor.magenta if cluster in self.busy_clusters else Qt.GlobalColor.gray

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = FileBrowserApp()
    window.show()
    sys.exit(app.exec())
