# src/fatfloppy/gui/main_window.py
import os
import re
import copy
import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtGui import (QAction, QFont, QPalette)
from PyQt6.QtWidgets import (QDockWidget, QFileDialog, QInputDialog, QLabel,
                             QMainWindow, QMessageBox, QToolBar,
                             QTreeWidget, QTreeWidgetItem, QHeaderView, QAbstractItemView,
                             QWidget, QVBoxLayout, QGroupBox, QApplication,
                             QPlainTextEdit, QPushButton, QAbstractItemView)

from ..core.controller import DiskController
from ..core.drivers import GREASEWEAZLE_AVAILABLE
from ..core.utils.logging_config import get_logger
from .dialogs import DriveSelectionDialog, CreateImageDialog
from .disk_map import DiskMapView
from .file_browser import DragDropTreeWidget
from .models import FileSystemNode
from .themes import get_dark_theme, get_light_theme

class FileBrowserApp(QMainWindow):
    logger = get_logger(__name__)

    def __init__(self):
        super().__init__()
        self.root_node = None
        self.current_node = None
        self.current_path = "/"
        self.current_head = 0
        self.busy_units = []
        self.free_space = 0
        self.total_space = 0
        self.controller = None
        self.current_file_path = None
        self.greaseweazle_available = GREASEWEAZLE_AVAILABLE

        self.initUI()
        self.setup_fonts()
        self.update_theme()
        QApplication.instance().styleHints().colorSchemeChanged.connect(self.update_theme)
        self.reset_ui()

    def initUI(self):
        self.setWindowTitle("FatFloppy Disk Browser")
        self.setGeometry(100, 100, 1200, 800)

        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")

        create_image_action = QAction("Create Disk Image", self)
        create_image_action.triggered.connect(self.create_disk_image)
        file_menu.addAction(create_image_action)

        open_image_action = QAction("Open Disk Image File", self)
        open_image_action.setToolTip("Open a disk image file (.ima, .img, .imd)")
        open_image_action.triggered.connect(self.open_disk_image_file)
        file_menu.addAction(open_image_action)

        open_floppy_action = QAction("Open Physical Floppy", self)
        open_floppy_action.triggered.connect(self.open_physical_floppy)
        if not self.greaseweazle_available:
            open_floppy_action.setEnabled(False)
        file_menu.addAction(open_floppy_action)

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        self.toolbar = QToolBar("Main Toolbar", self)
        self.addToolBar(self.toolbar)

        self.head_action = QAction("Switch to head 1", self)
        self.head_action.setToolTip("Switch between disk heads (sides)")
        self.head_action.triggered.connect(self.toggle_head)
        self.toolbar.addAction(self.head_action)
        self.toolbar.addSeparator()

        extract_action = QAction("Extract", self)
        extract_action.setToolTip("Extract selected item(s) to local filesystem")
        extract_action.triggered.connect(self.extract_selected_items)
        self.toolbar.addAction(extract_action)

        delete_action = QAction("Delete", self)
        delete_action.setToolTip("Delete selected file(s) or directory")
        delete_action.triggered.connect(self.delete_selected_items)
        self.toolbar.addAction(delete_action)

        create_dir_action = QAction("New Folder", self)
        create_dir_action.setToolTip("Create a new directory in current location")
        create_dir_action.triggered.connect(self.create_directory)
        self.toolbar.addAction(create_dir_action)

        add_file_action = QAction("Add File", self)
        add_file_action.setToolTip("Add a file to current directory")
        add_file_action.triggered.connect(self.add_file)
        self.toolbar.addAction(add_file_action)

        self.tree_dock = QDockWidget("Directory Tree", self)
        self.tree_widget = QTreeWidget()
        self.tree_widget.setHeaderLabel("Directories")
        self.tree_widget.itemClicked.connect(self.select_directory)
        self.tree_dock.setWidget(self.tree_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.tree_dock)

        self.disk_info_dock = QDockWidget("Disk Information", self)
        disk_info_widget = QWidget()
        disk_info_layout = QVBoxLayout(disk_info_widget)

        self.physical_format_group = QGroupBox("Physical Geometry")
        self.physical_format_info = QLabel("No disk image loaded")
        geometry_layout = QVBoxLayout(self.physical_format_group)
        geometry_layout.addWidget(self.physical_format_info)
        self.physical_format_group.setLayout(geometry_layout)

        self.filesystem_group = QGroupBox("Filesystem")
        self.filesystem_info = QLabel("No filesystem detected")
        filesystem_layout = QVBoxLayout(self.filesystem_group)
        filesystem_layout.addWidget(self.filesystem_info)
        self.filesystem_group.setLayout(filesystem_layout)

        disk_info_layout.addWidget(self.physical_format_group)
        disk_info_layout.addWidget(self.filesystem_group)
        disk_info_layout.setContentsMargins(2, 2, 2, 2)
        disk_info_layout.setSpacing(4)

        disk_info_widget.setLayout(disk_info_layout)
        self.disk_info_dock.setWidget(disk_info_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.disk_info_dock)

        self.splitDockWidget(self.tree_dock, self.disk_info_dock, Qt.Orientation.Vertical)
        self.resizeDocks([self.tree_dock, self.disk_info_dock], [640, 160], Qt.Orientation.Vertical)

        self.file_list_dock = QDockWidget("Files in Current Directory", self)
        self.file_list = DragDropTreeWidget(self)
        self.file_list.setHeaderLabels(["Name", "Size", "Date/Time", "Attr"])
        self.file_list.setDragEnabled(True)
        self.file_list.setAcceptDrops(True)
        self.file_list.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_list_dock.setWidget(self.file_list)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.file_list_dock)

        header = self.file_list.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        # Setup separate dock widgets for disk map and text viewer
        self.disk_map_dock = QDockWidget("Disk Map", self)
        self.disk_map = DiskMapView(self)
        self.disk_map_view = self.disk_map.view
        self.disk_map_dock.setWidget(self.disk_map_view)

        self.text_viewer_dock = QDockWidget("Text Viewer", self)
        text_viewer_widget = QWidget()
        text_viewer_layout = QVBoxLayout(text_viewer_widget)
        self.text_viewer = QPlainTextEdit()
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self.save_file)

        text_viewer_layout.addWidget(self.text_viewer)
        text_viewer_layout.addWidget(self.save_button)
        self.text_viewer_dock.setWidget(text_viewer_widget)

        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.disk_map_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.text_viewer_dock)
        self.tabifyDockWidget(self.disk_map_dock, self.text_viewer_dock)
        self.disk_map_dock.raise_()

        self.file_list.selectionModel().selectionChanged.connect(self.on_selection_changed)

        self.statusBar().showMessage("Ready")

    def setup_fonts(self):
        monospace_fonts = [
            "Courier New", "DejaVu Sans Mono", "Consolas", "Menlo", "Liberation Mono", "Monaco"
        ]
        self.app_font = QFont()
        self.app_font.setFamily(monospace_fonts[0])
        self.app_font.setStyleHint(QFont.StyleHint.Monospace)
        self.app_font.setFixedPitch(True)
        self.app_font.setPointSize(12)
        self.setFont(self.app_font)

        self.tree_widget.setFont(self.app_font)
        self.file_list.setFont(self.app_font)
        self.physical_format_info.setFont(self.app_font)
        self.filesystem_info.setFont(self.app_font)
        self.text_viewer.setFont(self.app_font)

    def update_theme(self):
        color_scheme = QApplication.styleHints().colorScheme()
        if color_scheme == Qt.ColorScheme.Dark:
            style_sheet = get_dark_theme()
        elif color_scheme == Qt.ColorScheme.Light:
            style_sheet = get_light_theme()
        else:
            style_sheet = get_dark_theme()
        self.setStyleSheet(style_sheet)

    def reset_ui(self):
        self.root_node = None
        self.current_node = None
        self.current_path = "/"
        self.current_head = 0
        self.busy_units = []
        self.free_space = 0
        self.total_space = 0
        self.current_file_path = None

        if self.controller:
            self.controller.close_disk()
        self.controller = None
        self.tree_widget.clear()
        self.file_list.clear()
        self.physical_format_info.setText("No disk image loaded")
        self.filesystem_info.setText("No filesystem detected")
        self.disk_map.scene.clear()
        self.draw_disk_map()
        self.head_action.setEnabled(False)
        self.head_action.setText("Switch to Head 1")
        self.statusBar().showMessage("Ready")
        self.text_viewer.clear()
        self.disk_map_dock.raise_()

    def on_selection_changed(self, selected, deselected):
        selected_items = self.file_list.selectedItems()
        if len(selected_items) == 1:
            item = selected_items[0]
            node = item.node
            if not node.is_dir:
                file_path = self.build_full_path(node.name)
                if node.size <= 2048:
                    content = self.controller.read_file(file_path)
                    if content is not None and self.is_text_file(content):
                        self.text_viewer.setPlainText(content.decode('cp437'))
                        self.text_viewer_dock.raise_()
                        self.current_file_path = file_path
                        return

        self.text_viewer.setPlainText("")
        self.disk_map_dock.raise_()
        self.current_file_path = None

    def is_text_file(self, content, check_bytes=4096):
        sample = content[:check_bytes]
        try:
            text = sample.decode('cp437')
            return all(c.isprintable() or c in '\n\t' for c in text)
        except UnicodeDecodeError:
            return False

    def save_file(self):
        if self.current_file_path:
            try:
                content = self.text_viewer.toPlainText().encode('cp437')
                success = self.controller.write_file(self.current_file_path, content)
                if success:
                    self.statusBar().showMessage(f"Saved changes to {self.current_file_path}")
                else:
                    QMessageBox.warning(self, "Warning", "Failed to save file")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save file: {str(e)}")
        else:
            QMessageBox.warning(self, "Warning", "No file selected to save")

    def refresh_filesystem_ui(self, preserve_path=None):
        self.root_node = self.build_fs_tree()
        self.populate_tree()

        if preserve_path:
            self.navigate_to_path(preserve_path)
        else:
            self.current_node = self.root_node
            self.current_path = "/"
            self.update_file_list()

        self.get_busy_units()
        self.update_disk_info()
        self.draw_disk_map()
        self.statusBar().showMessage(f"Current path: {self.current_path}")

    def create_disk_image(self):
        dialog = CreateImageDialog(self)
        if not dialog.exec():
            return

        try:
            file_path, format_info, volume_label, output_format = dialog.get_selection()
        except ValueError as e:
            QMessageBox.warning(self, "Warning", str(e))
            return

        try:
            profile = self.get_format_profile(format_info)
            if not profile:
                QMessageBox.critical(self, "Error", "Failed to determine format profile for creation.")
                return
            if not profile.physical_format or not profile.filesystem_metadata:
                QMessageBox.critical(self, "Error", "Selected format profile is incomplete for creation.")
                return

            self.controller = DiskController()
            self.statusBar().showMessage(f"Creating and formatting disk image: {file_path}...")
            QApplication.processEvents()

            if self.controller.create_and_format_image(file_path, profile, volume_label, output_format):
                self.root_node = self.build_fs_tree()
                self.current_node = self.root_node
                self.current_path = "/"
                self.refresh_filesystem_ui()

                if self.controller.physical_format and self.controller.physical_format.heads > 1:
                    self.head_action.setEnabled(True)
                    self.head_action.setText(f"Switch to Head {1 - self.current_head}")
                else:
                    self.head_action.setEnabled(False)
                    self.head_action.setText("Single-sided disk")

                self.statusBar().showMessage(f"Created and formatted disk image: {file_path}")
            else:
                self.reset_ui()
                QMessageBox.critical(self, "Error", "Failed to create and format disk image.")
        except Exception as e:
            self.reset_ui()
            QMessageBox.critical(self, "Error", f"An unexpected error occurred: {str(e)}")

    def open_disk_image_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Open Disk Image", "", "Disk Images (*.ima *.img *.imd);;All Files (*)")
        if not file_path:
            return

        _, ext = os.path.splitext(file_path)
        disk_type = "IMG"
        if ext.lower() == ".imd":
            disk_type = "IMD"

        try:
            self.reset_ui()
            self.controller = DiskController()
            self.statusBar().showMessage(f"Opening {disk_type} disk: {file_path}...")
            QApplication.processEvents()

            if self.controller.open_disk(file_path, disk_type):
                self.refresh_filesystem_ui()

                if self.controller.physical_format and self.controller.physical_format.heads > 1:
                    self.head_action.setEnabled(True)
                    self.head_action.setText(f"Switch to Head {1 - self.current_head}")
                else:
                    self.head_action.setEnabled(False)
                    self.head_action.setText("Single-sided disk")
                self.statusBar().showMessage(f"Loaded: {file_path} (Type: {disk_type})")
            else:
                self.reset_ui()
                QMessageBox.critical(self, "Error", f"Failed to open {disk_type} disk image")
        except ValueError as e:
             self.reset_ui()
             QMessageBox.critical(self, "IMD Error", f"Failed to parse IMD file: {str(e)}")
        except Exception as e:
            self.reset_ui()
            QMessageBox.critical(self, "Error", f"Failed to open disk image: {str(e)}")

    def open_physical_floppy(self):
        try:
            dialog = DriveSelectionDialog(self)
            if not dialog.exec():
                return

            drive_letter, drive_size, format_info = dialog.get_selection()
            self.controller = DiskController()
            if self.controller.open_disk(None, "physical", drive_letter=drive_letter, drive_size=drive_size, format_info=format_info):
                self.root_node = self.build_fs_tree()
                self.current_node = self.root_node
                self.current_path = "/"
                self.refresh_filesystem_ui()

                if self.controller.physical_format and self.controller.physical_format.heads > 1:
                    self.head_action.setEnabled(True)
                    self.head_action.setText(f"Switch to Head {1 - self.current_head}")
                else:
                    self.head_action.setEnabled(False)
                    self.head_action.setText("Single-sided disk")

                format_name = self.controller.detect_format()
                format_text = f" using {format_name}" if format_name else ""
                if format_info and not format_info.get("profile_name"):
                    format_text += f" (Custom format: {format_info.get('cylinders')}x{format_info.get('heads')}x{format_info.get('sectors_per_track')})"
                self.statusBar().showMessage(f"Loaded physical floppy{format_text} (Drive: {drive_letter}, Size: {drive_size}\")")
            else:
                self.reset_ui()
                QMessageBox.critical(self, "Error", "Failed to open physical floppy")
        except Exception as e:
            self.reset_ui()
            QMessageBox.critical(self, "Error", f"Failed to open physical floppy: {str(e)}")

    def get_format_profile(self, format_info):
        if "profile_name" in format_info:
            profile_name = format_info["profile_name"]
            from ..core.format_definitions import FLOPPY_FORMATS
            original_profile = FLOPPY_FORMATS.get(profile_name)
            if original_profile:
                return copy.deepcopy(original_profile)
            else:
                QMessageBox.critical(self, "Error", f"Predefined format '{profile_name}' not found.")
                return None
        else:
            if not self.controller:
                self.controller = DiskController()
            return self.controller.create_custom_profile(format_info)

    def update_disk_info(self):
        self.update_geometry_info()
        self.update_filesystem_info()

    def update_geometry_info(self):
        if not self.controller or not self.controller.physical_format:
            self.physical_format_info.setText("Disk geometry not available")
            return

        geometry = self.controller.physical_format
        total_sectors = geometry.total_sectors
        total_bytes = total_sectors * geometry.bytes_per_sector

        imd_comment = ""
        if hasattr(self.controller.driver, 'comment'):
            imd_comment = self.controller.driver.comment
            if imd_comment:
                 display_comment = (imd_comment[:60] + '...') if len(imd_comment) > 63 else imd_comment
                 imd_comment = f"IMD Comment: {display_comment}\n"

        if geometry.track_formats:
            encodings = set(tf.encoding for tf in geometry.track_formats)
            rates = set(tf.rate for tf in geometry.track_formats)
            spts = set(tf.sectors_per_track for tf in geometry.track_formats)
            encoding_text = list(encodings)[0] if len(encodings) == 1 else "variable"
            rate_text = f"{list(rates)[0]} kbps" if len(rates) == 1 else "variable"
            spt_text = str(list(spts)[0]) if len(spts) == 1 else "variable"
        else:
            encoding_text = "N/A"
            rate_text = "N/A"
            spt_text = "N/A"

        info = (
            f"{imd_comment}"
            f"Encoding: {encoding_text}\n"
            f"Data Rate: {rate_text}\n"
            f"Rotation Speed: {geometry.rpm} RPM\n"
            f"Bytes per Sector: {geometry.bytes_per_sector}\n"
            f"Sectors per Track: {spt_text}\n"
            f"Number of Heads: {geometry.heads}\n"
            f"Number of Cylinders: {geometry.cylinders}\n"
            f"Total Sectors: {total_sectors}\n"
            f"Total Size: {total_bytes / 1024:.1f} KB"
        )
        self.physical_format_info.setText(info)

    def update_filesystem_info(self):
        if not self.controller:
            self.filesystem_info.setText("No disk loaded")
            return

        if self.controller.filesystem:
            fs_type = type(self.controller.filesystem).__name__.replace("Filesystem", "")
            fs_info_dict = self.controller.filesystem.get_display_info()

            fs_info_lines = []
            if "Filesystem Type" in fs_info_dict:
                 fs_info_lines.append(f"Filesystem Type: {fs_info_dict.pop('Filesystem Type')}")
            if "Volume Label" in fs_info_dict:
                 fs_info_lines.append(f"Volume Label: {fs_info_dict.pop('Volume Label')}")
            for key, value in fs_info_dict.items():
                fs_info_lines.append(f"{key}: {value}")

            fs_details = "\n".join(fs_info_lines)

            space_info = self.controller.get_free_space()
            if space_info:
                free_bytes, total_bytes = space_info
                free_kb = free_bytes / 1024
                total_kb = total_bytes / 1024
                percent_free = (free_bytes / total_bytes * 100) if total_bytes > 0 else 0
                space_str = f"Free Space: {free_kb:.1f} KB / {total_kb:.1f} KB ({percent_free:.1f}%)"
            else:
                space_str = "Free Space: N/A"

            info = f"{fs_details}\n{space_str}"
            self.filesystem_info.setText(info)
        else:
            self.filesystem_info.setText("No filesystem detected")

    def build_fs_tree(self):
        if not self.controller:
            return None

        root_node = FileSystemNode("Root", is_dir=True, attributes="-")
        all_directories = ["/"]
        directory_contents = {}
        root_items = self.controller.list_directory("/")
        for item in root_items:
            if item["is_dir"]:
                path = "/" + item["name"]
                all_directories.append(path)

        for directory in all_directories:
            directory_contents[directory] = self.controller.list_directory(directory)
            for item in directory_contents[directory]:
                if item["is_dir"]:
                    if directory == "/":
                        path = f"/{item['name']}"
                    else:
                        path = f"{directory}/{item['name']}"
                    if path not in all_directories:
                        all_directories.append(path)

        node_dict = {"/": root_node}
        all_dirs = []
        for dir_path in all_directories:
            if dir_path == "/":
                continue

            parts = dir_path.strip("/").split("/")
            parent_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
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

        all_dirs.sort(key=lambda x: len(x["path"].split("/")))

        for dir_data in all_dirs:
            path = dir_data["path"]
            parent_path = dir_data["parent_path"]
            name = dir_data["name"]
            dir_info = dir_data["info"]

            full_path = (parent_path + "/" + name).replace("//", "/")
            if full_path in node_dict:
                continue

            parent = node_dict.get(parent_path)
            if not parent:
                continue

            node = FileSystemNode(
                name=name,
                size=0,
                is_dir=True,
                modified=dir_info["datetime"].strftime("%Y-%m-%d %H:%M:%S") if isinstance(dir_info["datetime"], datetime.datetime) else str(dir_info["datetime"]),
                attributes=dir_info["attributes"],
                parent=parent
            )

            parent.appendChild(node)
            node_dict[path] = node

        for dir_path, items in directory_contents.items():
            parent = node_dict.get(dir_path)
            if not parent:
                continue

            for item in items:
                if not item["is_dir"]:
                    node = FileSystemNode(
                        name=item["name"],
                        size=item["size"],
                        is_dir=False,
                        modified=item["datetime"].strftime("%Y-%m-%d %H:%M:%S") if isinstance(item["datetime"], datetime.datetime) else str(item["datetime"]),
                        attributes=item["attributes"],
                        parent=parent
                    )
                    parent.appendChild(node)
        return root_node

    def populate_tree(self):
        self.tree_widget.clear()
        if not self.root_node:
            return

        root_item = QTreeWidgetItem(self.tree_widget, ["Root"])
        root_item.node = self.root_node
        self._populate_tree(self.root_node, root_item)
        self.tree_widget.expandAll()

    def _populate_tree(self, node, parent_item):
        if not node:
            return

        for child in node.children:
            if child.is_dir:
                child_item = QTreeWidgetItem(parent_item, [child.name])
                child_item.node = child
                self._populate_tree(child, child_item)

    def select_directory(self, item):
        self.current_node = item.node
        self.current_path = self.build_path_from_node(item.node)
        self.update_file_list()
        self.statusBar().showMessage(f"Viewing: {self.current_path}")

    def build_path_from_node(self, node):
        path_parts = []
        curr = node
        while curr and curr.parent:
            path_parts.insert(0, curr.name)
            curr = curr.parent
        return "/" + "/".join(path_parts) if path_parts else "/"

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

    def extract_selected_items(self):
        selected_items = self.file_list.selectedItems()
        if not selected_items or not self.controller:
            return

        if len(selected_items) == 1:
            item = selected_items[0]
            node = item.node
            source_path = self.build_full_path(node.name)
            if node.is_dir:
                base_dir = QFileDialog.getExistingDirectory(self, "Select Directory to Extract To")
                if not base_dir:
                    return
                local_dir_path = os.path.join(base_dir, node.name)
                self.extract_directory(source_path, local_dir_path)
            else:
                save_path, _ = QFileDialog.getSaveFileName(self, "Save File", node.name)
                if not save_path:
                    return
                self.extract_file(source_path, save_path)
        else:
            base_dir = QFileDialog.getExistingDirectory(self, "Select Directory to Extract To")
            if not base_dir:
                return
            for item in selected_items:
                node = item.node
                source_path = self.build_full_path(node.name)
                if node.is_dir:
                    local_dir_path = os.path.join(base_dir, node.name)
                    self.extract_directory(source_path, local_dir_path)
                else:
                    local_file_path = os.path.join(base_dir, node.name)
                    self.extract_file(source_path, local_file_path)

    def extract_file(self, source_path, local_path):
        try:
            file_data = self.controller.read_file(source_path)
            if file_data is not None:
                with open(local_path, 'wb') as f:
                    f.write(file_data)
                self.statusBar().showMessage(f"Extracted {source_path} to {local_path}")
            else:
                QMessageBox.warning(self, "Warning", f"Failed to read file data for {source_path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to extract file {source_path}: {str(e)}")

    def extract_directory(self, source_dir_path, local_dir_path):
        try:
            os.makedirs(local_dir_path, exist_ok=True)
            items = self.controller.list_directory(source_dir_path)
            for item in items:
                item_name = item["name"]
                if item_name in [".", ".."]:
                    continue
                item_source_path = f"{source_dir_path}/{item_name}" if source_dir_path != "/" else f"/{item_name}"
                item_local_path = os.path.join(local_dir_path, item_name)
                if item["is_dir"]:
                    self.extract_directory(item_source_path, item_local_path)
                else:
                    self.extract_file(item_source_path, item_local_path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to extract directory {source_dir_path}: {str(e)}")

    def delete_selected_items(self):
        selected_items = self.file_list.selectedItems()
        if not selected_items or not self.controller:
            return

        paths_to_delete = [self.build_full_path(item.node.name) for item in selected_items]

        if len(paths_to_delete) == 1:
            msg = f"Are you sure you want to delete '{paths_to_delete[0]}'?"
        else:
            msg = f"Are you sure you want to delete {len(paths_to_delete)} items?"
        reply = QMessageBox.question(self, "Confirm Deletion", msg,
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        failed_paths = []
        for path in paths_to_delete:
            success = self.controller.delete_item_recursive(path)
            if not success:
                failed_paths.append(path)

        current_path = self.current_path
        self.refresh_filesystem_ui(current_path)

        if not failed_paths:
            self.statusBar().showMessage(f"Deleted {len(paths_to_delete)} item(s)")
        else:
            failed_msg = "Failed to delete:\n" + "\n".join(failed_paths)
            QMessageBox.warning(self, "Deletion Failed", failed_msg)
            self.statusBar().showMessage(f"Deleted {len(paths_to_delete) - len(failed_paths)} item(s), {len(failed_paths)} failed")

    def navigate_to_path(self, path):
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
                self.current_node = self.root_node
                self.current_path = "/"
                self.update_file_list()
                return False

        self.current_node = current
        self.current_path = path
        self.update_file_list()
        self.select_tree_item_by_path(path)
        return True

    def select_tree_item_by_path(self, path):
        if path == "/":
            if self.tree_widget.topLevelItemCount() > 0:
                self.tree_widget.setCurrentItem(self.tree_widget.topLevelItem(0))
            return

        parts = path.strip("/").split("/")
        if self.tree_widget.topLevelItemCount() == 0:
            return

        item = self.tree_widget.topLevelItem(0)

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
        if not self.controller or not self.current_node:
            QMessageBox.warning(self, "Warning", "No disk image loaded")
            return

        current_path = self.current_path
        dir_name, ok = QInputDialog.getText(self, "Create New Directory",
                                          "Enter directory name (8.3 format):")
        if not ok or not dir_name:
            return

        try:
            success = self.controller.create_directory(current_path + "/" + dir_name)
            if success:
                self.refresh_filesystem_ui(current_path)
                self.statusBar().showMessage(f"Created directory {dir_name} in {current_path}")
            else:
                QMessageBox.warning(self, "Warning", f"Failed to create directory {dir_name}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to create directory: {str(e)}")

    def import_path(self, local_path, target_path, auto_name):
        local_path = os.path.normpath(local_path)

        if os.path.isfile(local_path):
            if not auto_name:
                base_name = os.path.basename(local_path)
                base_name = self.format_83_filename(base_name)
                new_name, ok = QInputDialog.getText(self, "File Name",
                                                    f"Enter file name for {base_name} (8.3 format):",
                                                    text=base_name)
                if not ok or not new_name:
                    return
                existing_names = [item['name'].upper() for item in self.controller.list_directory(target_path)]
                if new_name.upper() in existing_names:
                    QMessageBox.warning(self, "Warning", f"File '{new_name}' already exists in {target_path}")
                    return
                try:
                    self.add_file_to_disk(local_path, new_name, target_path)
                except Exception as e:
                    QMessageBox.critical(self, "Error", f"Failed to add file: {str(e)}")
            else:
                original_name = os.path.basename(local_path)
                new_name = self.generate_unique_83_name(original_name, target_path, is_dir=False)
                try:
                    self.add_file_to_disk(local_path, new_name, target_path)
                except Exception as e:
                    QMessageBox.critical(self, "Error", f"Failed to add file: {str(e)}")
        elif os.path.isdir(local_path):
            original_name = os.path.basename(local_path)
            if not original_name:
                QMessageBox.critical(self, "Error", f"Invalid directory name: empty name for path {local_path}")
                return
            new_dir_name = self.generate_unique_83_name(original_name, target_path, is_dir=True)
            new_dir_path = f"{target_path}/{new_dir_name}" if target_path != "/" else f"/{new_dir_name}"
            try:
                success = self.controller.create_directory(new_dir_path)
                if not success:
                    raise Exception("Failed to create directory")
                for item in os.listdir(local_path):
                    item_path = os.path.join(local_path, item)
                    self.import_path(item_path, new_dir_path, auto_name=True)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to import directory: {str(e)}")

    def generate_unique_83_name(self, original_name, target_path, is_dir):
        def to_83_name(name):
            name = name.upper()
            name = re.sub(r'[\\/:*?"<>|\s+]', '_', name)
            if '.' in name and not is_dir:
                base, ext = name.rsplit('.', 1)
                base = base[:8]
                ext = ext[:3]
                return f"{base}.{ext}"
            else:
                return name[:8]

        existing_names = [item['name'].upper() for item in self.controller.list_directory(target_path)]
        base_name = to_83_name(original_name)
        if base_name not in existing_names:
            return base_name
        if '.' in base_name and not is_dir:
            base, ext = base_name.rsplit('.', 1)
            base = base[:6]
            counter = 1
            while True:
                new_base = f"{base}~{counter}"
                new_name = f"{new_base}.{ext}"
                if new_name not in existing_names:
                    return new_name
                counter += 1
                if counter > 999:
                    raise ValueError("Cannot generate unique name")
        else:
            base = base_name[:6]
            counter = 1
            while True:
                new_name = f"{base}~{counter}"
                if new_name not in existing_names:
                    return new_name
                counter += 1
                if counter > 999:
                    raise ValueError("Cannot generate unique name")

    def format_83_filename(self, filename):
        if len(filename) > 12 or filename.count('.') > 1:
            parts = filename.split('.')
            if len(parts) > 1:
                return parts[0][:8] + '.' + parts[-1][:3]
            else:
                return parts[0][:8]
        return filename.upper()

    def build_full_path(self, name):
        path = self.current_path
        if path != "/":
            path += "/"
        return path + name

    def add_file(self):
        if not self.controller or not self.current_node:
            QMessageBox.warning(self, "Warning", "No disk image loaded")
            return

        file_path, _ = QFileDialog.getOpenFileName(self, "Select File to Add")
        if not file_path:
            return

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
        with open(file_path, 'rb') as f:
            file_data = f.read()

        full_path = f"{dest_path}{'/' if not dest_path.endswith('/') else ''}{dest_name}"
        success = self.controller.write_file(full_path, file_data)
        if success:
            self.refresh_filesystem_ui(dest_path)
            self.statusBar().showMessage(f"Added file {dest_name} to {dest_path}")
        else:
            QMessageBox.warning(self, "Warning", f"Failed to add file {dest_name}")

    def toggle_head(self):
        if self.controller and self.controller.physical_format.heads > 1:
            self.current_head = 1 - self.current_head
            self.head_action.setText(f"Switch to Head {1 - self.current_head}")
            self.draw_disk_map()
        else:
            QMessageBox.information(self, "Info", "Head switching is not available for this disk.")

    def get_busy_units(self):
        if not self.controller:
            self.busy_units = []
            self.free_space = 0
            self.total_space = 0
            return

        try:
            self.busy_units = self.controller.get_allocated_units()
            unit_name = "units"
            allocation_unit_size_bytes = 512

            if self.controller.filesystem:
                if self.controller.filesystem.allocation_unit_size > 0:
                    allocation_unit_size_bytes = self.controller.filesystem.allocation_unit_size

                space_info = self.controller.get_free_space()
                if space_info:
                    free_bytes, total_bytes = space_info
                    if allocation_unit_size_bytes > 0:
                        self.free_space = free_bytes // allocation_unit_size_bytes
                        self.total_space = total_bytes // allocation_unit_size_bytes
                    else:
                        self.logger.warning("Allocation unit size is zero, cannot calculate space in units.")
                        self.free_space = 0
                        self.total_space = 0

                else:
                    self.logger.warning("Could not retrieve free space information.")
                    self.free_space = 0
                    if allocation_unit_size_bytes > 0 and self.controller.physical_format:
                        total_data_bytes_geom = self.controller.physical_format.total_bytes
                        self.total_space = total_data_bytes_geom // allocation_unit_size_bytes
                        self.free_space = max(0, self.total_space - len(self.busy_units))
                    else:
                        self.total_space = len(self.busy_units)
                        self.free_space = 0

            else:
                self.logger.warning("No filesystem detected, cannot calculate accurate free/total space.")
                self.busy_units = []
                self.free_space = 0
                self.total_space = 0


            self.logger.debug(f"Busy {unit_name}: {len(self.busy_units)}, Free: {self.free_space}, Total: {self.total_space}")

        except Exception as e:
            self.logger.error(f"Error getting busy units/space info: {e}", exc_info=True)
            self.busy_units = []
            self.free_space = 0
            self.total_space = 0

    def draw_disk_map(self):
        text_color = self.palette().color(QPalette.ColorRole.WindowText)
        self.disk_map.draw_disk_map(
            self.controller,
            self.current_head,
            self.busy_units,
            self.free_space,
            self.total_space,
            self.app_font,
            text_color,
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
