# src/fatfloppy/gui/main_window.py
import os
import copy
import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtGui import (QAction, QFont)
from PyQt6.QtWidgets import (QDockWidget, QFileDialog, QInputDialog, QLabel,
                             QMainWindow, QMessageBox, QToolBar,
                             QTreeWidget, QTreeWidgetItem, QHeaderView, QAbstractItemView,
                             QWidget, QVBoxLayout, QGroupBox, QApplication)

from ..core.controller import DiskController
from ..core.filesystem import FATFilesystem
from .dialogs import DriveSelectionDialog, CreateImageDialog
from .disk_map import DiskMapView
from .file_browser import DragDropTreeWidget
from .models import FileSystemNode

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
        # TODO: Should check for version too?
        try:
            import greaseweazle
            self.greaseweazle_available = True
        except ImportError:
            self.greaseweazle_available = False

        self.initUI()
        self.setup_fonts()

    def initUI(self):
        self.setWindowTitle("FatFloppy Disk Browser")
        self.setGeometry(100, 100, 1200, 800)

        menu_bar = self.menuBar()
        menu_bar.setStyleSheet("QMenuBar { min-height: 20px; max-height: 25px; }")
        file_menu = menu_bar.addMenu("File")
        file_menu.setStyleSheet("QMenu { padding: 5px; }")

        create_image_action = QAction("Create Disk Image", self)
        create_image_action.triggered.connect(self.create_disk_image)
        file_menu.addAction(create_image_action)

        open_image_action = QAction("Open Disk Image File", self)
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
        self.toolbar.setStyleSheet("QToolBar { spacing: 5px; min-height: 25px; max-height: 30px; }")
        self.addToolBar(self.toolbar)

        self.head_action = QAction("Switch to head 1", self)
        self.head_action.setToolTip("Switch between disk heads (sides)")
        self.head_action.triggered.connect(self.toggle_head)
        self.toolbar.addAction(self.head_action)
        self.toolbar.addSeparator()

        extract_action = QAction("Extract", self)
        extract_action.setToolTip("Extract selected file to local filesystem")
        extract_action.triggered.connect(self.extract_selected_file)
        self.toolbar.addAction(extract_action)

        delete_action = QAction("Delete", self)
        delete_action.setToolTip("Delete selected file or directory")
        delete_action.triggered.connect(self.delete_selected_item)
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

        self.geometry_group = QGroupBox("Physical Geometry")
        self.geometry_info = QLabel("No disk image loaded")
        geometry_layout = QVBoxLayout(self.geometry_group)
        geometry_layout.addWidget(self.geometry_info)
        self.geometry_group.setLayout(geometry_layout)

        self.filesystem_group = QGroupBox("Filesystem")
        self.filesystem_info = QLabel("No filesystem detected")
        filesystem_layout = QVBoxLayout(self.filesystem_group)
        filesystem_layout.addWidget(self.filesystem_info)
        self.filesystem_group.setLayout(filesystem_layout)

        disk_info_layout.addWidget(self.geometry_group)
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
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)          # Name column
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents) # Size column
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch) # Date/Time column
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch) # Attributes column

        self.disk_map_dock = QDockWidget("Disk Map", self)
        self.disk_map = DiskMapView(self)
        self.disk_map_view = self.disk_map.view
        self.disk_map_dock.setWidget(self.disk_map_view)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.disk_map_dock)

        self.splitDockWidget(self.file_list_dock, self.disk_map_dock, Qt.Orientation.Horizontal)
        self.resizeDocks([self.file_list_dock, self.disk_map_dock], [480, 720], Qt.Orientation.Horizontal)
        self.reset_ui()
        self.statusBar().showMessage("Ready")

    def reset_ui(self):
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
        self.geometry_info.setText("No disk image loaded")
        self.filesystem_info.setText("No filesystem detected")
        self.disk_map.scene.clear()
        self.disk_map.scene.addText("No disk image loaded").setPos(10, 10)
        self.head_action.setEnabled(False)
        self.head_action.setText("Switch to Head 1")
        self.statusBar().showMessage("Ready")

    def refresh_filesystem_ui(self, preserve_path=None):
        self.root_node = self.build_fs_tree()
        self.populate_tree()

        if preserve_path:
            self.navigate_to_path(preserve_path)
        else:
            self.current_node = self.root_node
            self.current_path = "/"
            self.update_file_list()

        self.get_busy_clusters()
        self.update_disk_info()
        self.draw_disk_map()
        self.statusBar().showMessage(f"Current path: {self.current_path}")

    def setup_fonts(self):
        monospace_fonts = [
            "Courier New",        # All platforms
            "DejaVu Sans Mono",   # Linux
            "Consolas",           # Windows
            "Menlo",              # macOS
            "Liberation Mono",    # Linux
            "Monaco",             # macOS
        ]
        self.app_font = QFont()
        self.app_font.setFamily(monospace_fonts[0])  # Start with first preference
        self.app_font.setStyleHint(QFont.StyleHint.Monospace)  # Hint to use monospace if first choice unavailable
        self.app_font.setFixedPitch(True)  # Ensure fixed pitch
        self.app_font.setPointSize(12)     # Set reasonable size
        self.setFont(self.app_font)

        self.tree_widget.setFont(self.app_font)
        self.file_list.setFont(self.app_font)
        self.geometry_info.setFont(self.app_font)
        self.filesystem_info.setFont(self.app_font)

        header_font = QFont(self.app_font)
        header_font.setBold(True)

    def create_disk_image(self):
        file_path, _ = QFileDialog.getSaveFileName(self, "Create Disk Image", "", "Disk Images (*.ima *.img)")
        if not file_path:
            return

        dialog = CreateImageDialog(self)
        if not dialog.exec():
            return

        format_info, volume_label = dialog.get_selection()

        try:
            profile = self.get_format_profile(format_info)
            if not profile:
                QMessageBox.critical(self, "Error", "Failed to determine format profile for creation.")
                return
            if not profile.physical_format or not profile.boot_sector:
                QMessageBox.critical(self, "Error", "Selected format profile is incomplete for creation (missing physical or boot sector details).")
                return

            self.controller = DiskController()
            self.statusBar().showMessage(f"Creating and formatting disk image: {file_path}...")
            QApplication.processEvents() # Update UI message

            if self.controller.create_and_format_image(file_path, profile, volume_label):
                self.root_node = self.build_fs_tree()
                self.current_node = self.root_node
                self.current_path = "/"
                self.refresh_filesystem_ui() # This updates info, draws map, populates tree/list etc.

                if self.controller.geometry and self.controller.geometry.heads > 1:
                    self.head_action.setEnabled(True)
                    self.head_action.setText(f"Switch to Head {1 - self.current_head}")
                else:
                    self.head_action.setEnabled(False)
                    self.head_action.setText("Single-sided disk")

                self.statusBar().showMessage(f"Created and formatted disk image: {file_path}")
            else:
                self.reset_ui() # Reset UI as controller state is likely invalid or closed
                QMessageBox.critical(self, "Error", "Failed to create and format disk image. Check logs for details.")

        except Exception as e:
            self.reset_ui()
            QMessageBox.critical(self, "Error", f"An unexpected error occurred during image creation: {str(e)}")

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

                if self.controller.geometry and self.controller.geometry.heads > 1:
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

                if self.controller.geometry and self.controller.geometry.heads > 1:
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
        # TODO: cleanup !!!
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
            from ..core.formats import FormatProfile, PhysicalFormat, FATVolumeInfo
            try:
                physical_format = PhysicalFormat(
                    encoding=format_info["encoding"],
                    rate=format_info["rate"],
                    rpm=format_info["rpm"],
                    gap3=format_info["gap3"],
                    cskew=format_info["cskew"],
                    interleave=format_info["interleave"],
                    cylinders=format_info["cylinders"],
                    heads=format_info["heads"],
                    sectors_per_track=format_info["sectors_per_track"],
                    bytes_per_sector=format_info["bytes_per_sector"],
                )

                # Construct FATVolumeInfo (boot sector) from custom parameters
                total_sectors = physical_format.total_sectors
                bytes_per_sector = physical_format.bytes_per_sector
                # Provide sensible defaults if parameters are missing, but log warnings
                # These calculations are crucial for a valid FAT structure

                sectors_per_cluster = format_info.get("sectors_per_cluster")
                if sectors_per_cluster is None:
                    # Default SPC based on size (very basic heuristic)
                    if total_sectors <= 655360 // bytes_per_sector: # <= 640KB ish
                         sectors_per_cluster = 1
                    elif total_sectors <= 1.44 * 1024 * 1024 // bytes_per_sector: # <= 1.44MB ish
                         sectors_per_cluster = 2
                    else:
                         sectors_per_cluster = 4 # Larger disks
                    print(f"Warning: sectors_per_cluster not provided for custom format, defaulting to {sectors_per_cluster}")
                if sectors_per_cluster <= 0: raise ValueError("sectors_per_cluster must be positive")


                reserved_sectors = format_info.get("reserved_sectors", 1)
                if reserved_sectors <= 0: raise ValueError("reserved_sectors must be positive")

                num_fats = format_info.get("num_fats", 2)
                if num_fats not in [1, 2]: raise ValueError("num_fats must be 1 or 2")

                root_entries = format_info.get("root_entries")
                if root_entries is None:
                     # Default root entries (common values)
                     if physical_format.total_bytes <= 360 * 1024: root_entries = 112
                     elif physical_format.total_bytes <= 1.44 * 1024 * 1024 : root_entries = 224
                     else: root_entries = 512 # Larger disks might use FAT16/32 conventions implicitly
                     print(f"Warning: root_entries not provided for custom format, defaulting to {root_entries}")
                if root_entries <= 0 or root_entries % 16 != 0: # Must align
                    raise ValueError("root_entries must be positive and typically a multiple of 16")

                sectors_per_fat = format_info.get("sectors_per_fat")
                if sectors_per_fat is None:
                     # Estimate sectors_per_fat (FAT12 specific calculation)
                     root_dir_sectors = (root_entries * 32 + bytes_per_sector - 1) // bytes_per_sector
                     first_data_sector_approx = reserved_sectors + (num_fats * 1) + root_dir_sectors # Initial guess
                     if first_data_sector_approx >= total_sectors:
                           raise ValueError("Calculated disk overhead exceeds total sectors.")
                     data_sectors_approx = total_sectors - first_data_sector_approx
                     if data_sectors_approx < 0 : data_sectors_approx = 0 # Handle edge case
                     num_clusters = data_sectors_approx // sectors_per_cluster
                     FAT12_MAX_CLUSTERS = 4084
                     if num_clusters > FAT12_MAX_CLUSTERS:
                         print(f"Warning: Calculated cluster count ({num_clusters}) exceeds FAT12 max. Filesystem might be FAT16.")
                         # Adjust for FAT16 if needed (2 bytes per entry)
                         bytes_per_fat_approx = (num_clusters * 2) + 4 # Rough estimate for FAT16/32 Boot sector
                     else:
                         # FAT12 entry = 1.5 bytes
                         bytes_per_fat_approx = int(num_clusters * 1.5) + 3 # Rough estimate

                     sectors_per_fat = (bytes_per_fat_approx + bytes_per_sector - 1) // bytes_per_sector
                     print(f"Warning: sectors_per_fat not provided, estimated as {sectors_per_fat}")
                if sectors_per_fat <= 0: raise ValueError("sectors_per_fat must be positive")

                # Recalculate total_sectors based on geometry if not matching format_info
                if total_sectors != format_info.get("total_sectors", total_sectors):
                     print(f"Warning: total_sectors in format_info ({format_info.get('total_sectors')}) differs from geometry ({total_sectors}). Using geometry.")


                boot_sector = FATVolumeInfo(
                    bytes_per_sector=bytes_per_sector,
                    sectors_per_cluster=sectors_per_cluster,
                    reserved_sectors=reserved_sectors,
                    num_fats=num_fats,
                    root_entries=root_entries,
                    total_sectors=total_sectors,
                    media_descriptor=format_info.get("media_descriptor", 0xF0), # Default F0 for 3.5" HD
                    sectors_per_fat=sectors_per_fat,
                    sectors_per_track=physical_format.sectors_per_track,
                    num_heads=physical_format.heads,
                    hidden_sectors=format_info.get("hidden_sectors", 0),
                    drive_number=format_info.get("drive_number", 0),
                    volume_serial=format_info.get("volume_serial", 0), # Default 0, usually generated
                    # volume_label and fs_type will be set during format
                )

                return FormatProfile(
                    name="custom", # Use a consistent name for custom profiles
                    description=f"Custom {physical_format.cylinders}x{physical_format.heads}x{physical_format.sectors_per_track}x{physical_format.bytes_per_sector}",
                    physical_format=physical_format,
                    boot_sector=boot_sector
                )

            except KeyError as e:
                 QMessageBox.critical(self, "Error", f"Missing required parameter for custom format definition: {e}")
                 return None
            except ValueError as e:
                 QMessageBox.critical(self, "Error", f"Invalid parameter for custom format definition: {e}")
                 return None
            except Exception as e:
                 QMessageBox.critical(self, "Error", f"Error creating custom format profile: {e}")
                 return None

    def update_disk_info(self):
        self.update_geometry_info()
        self.update_filesystem_info()

    def update_geometry_info(self):
        if not self.controller or not self.controller.geometry:
            self.geometry_info.setText("Disk geometry not available")
            return

        geometry = self.controller.geometry
        total_sectors = geometry.cylinders * geometry.heads * geometry.sectors_per_track
        total_bytes = total_sectors * geometry.bytes_per_sector
        format_info = "Unknown"
        format_result = self.controller.detect_format()
        if format_result:  # Check if tuple exists
            format_name, boot_data = format_result  # Unpack it
            format_profile = self.controller.get_format_by_name(format_name)
            if format_profile:
                format_info = format_profile.description

        info = (
            f"Format: {format_info}\n"
            f"Encoding: {geometry.encoding}\n"
            f"Data Rate: {geometry.rate} kbps\n"
            f"Rotation Speed: {geometry.rpm} RPM\n"
            f"Bytes per Sector: {geometry.bytes_per_sector}\n"
            f"Sectors per Track: {geometry.sectors_per_track}\n"
            f"Number of Heads: {geometry.heads}\n"
            f"Number of Cylinders: {geometry.cylinders}\n"
            f"Total Sectors: {total_sectors}\n"
            f"Total Size: {total_bytes / 1024:.1f} KB"
        )

        self.geometry_info.setText(info)

    def update_filesystem_info(self):
        if not self.controller:
            self.filesystem_info.setText("No disk loaded")
            return

        if self.controller.filesystem:
            fs_type = type(self.controller.filesystem).__name__.replace("Filesystem", "")
            space_info = self.controller.get_free_space()
            if space_info:
                free_bytes, total_bytes = space_info
                free_kb = free_bytes / 1024
                total_kb = total_bytes / 1024
                percent_free = (free_bytes / total_bytes * 100) if total_bytes > 0 else 0
            else:
                free_kb = 0
                total_kb = 0
                percent_free = 0

            fs_info = ""
            if isinstance(self.controller.filesystem, FATFilesystem):
                bs = self.controller.filesystem.boot_sector
                if bs:
                    fs_info += f"Sectors per Cluster: {bs.sectors_per_cluster}\n"
                    fs_info += f"Root Directory Entries: {bs.root_entries}\n"
                    fs_info += f"Reserved Sectors: {bs.reserved_sectors}\n"
                    fs_info += f"Number of FATs: {bs.num_fats}\n"
                    fs_info += f"Sectors per FAT: {bs.sectors_per_fat}\n"
                    fs_info += f"Media Descriptor: 0x{bs.media_descriptor:02X}\n"
                    if bs.volume_label.strip():
                        fs_info += f"Volume Label: {bs.volume_label}\n"
                    if bs.fs_type.strip():
                        fs_info += f"Filesystem Type: {bs.fs_type}\n"

            info = (
                f"Filesystem: {fs_type}\n"
                f"{fs_info}"
                f"Free Space: {free_kb:.1f} KB / {total_kb:.1f} KB ({percent_free:.1f}%)"
            )
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
                continue  # Skip if parent not found

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
        while curr and curr.parent:  # Don't include "Root" in the path
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

    def extract_selected_file(self):
        selected_items = self.file_list.selectedItems()
        if not selected_items or not self.controller:
            return

        item = selected_items[0]
        node = item.node

        if node.is_dir:
            QMessageBox.information(self, "Info", "Cannot extract directories")
            return

        save_path, _ = QFileDialog.getSaveFileName(self, "Save File", node.name)
        if not save_path:
            return

        try:
            file_path = self.build_full_path(node.name)
            file_data = self.controller.read_file(file_path)
            if file_data:
                with open(save_path, 'wb') as f:
                    f.write(file_data)
                self.statusBar().showMessage(f"Extracted {node.name} to {save_path}")
            else:
                QMessageBox.warning(self, "Warning", "Failed to read file data")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to extract file: {str(e)}")

    def delete_selected_item(self):
        selected_items = self.file_list.selectedItems()
        if not selected_items or not self.controller:
            return

        item = selected_items[0]
        node = item.node

        try:
            item_path = self.build_full_path(node.name)
            msg_type = "directory" if node.is_dir else "file"
            if QMessageBox.question(self, "Confirm Deletion",
                                  f"Are you sure you want to delete the {msg_type} {node.name}?",
                                  QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                success = self.controller.delete_item(item_path)

                if success:
                    current_path = self.current_path
                    self.refresh_filesystem_ui(current_path)
                    self.statusBar().showMessage(f"Deleted {node.name}")
                else:
                    QMessageBox.warning(self, "Warning", f"Failed to delete {node.name}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to delete item: {str(e)}")

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
        if self.controller and self.controller.geometry.heads > 1:
            self.current_head = 1 - self.current_head
            self.head_action.setText(f"Switch to Head {1 - self.current_head}")
            self.draw_disk_map()
        else:
            QMessageBox.information(self, "Info", "Head switching is not available for this disk.")

    def get_busy_clusters(self):
        if not self.controller:
            self.busy_clusters = []
            self.free_space = 0
            self.total_space = 0
            return

        try:
            self.busy_clusters = self.controller.get_allocated_clusters()
            space_info = self.controller.get_free_space()
            if space_info:
                free_bytes, total_bytes = space_info
                if self.controller.geometry:
                    bytes_per_sector = self.controller.geometry.bytes_per_sector
                    sectors_per_cluster = 1
                    if self.controller.boot_sector:
                        sectors_per_cluster = self.controller.boot_sector.sectors_per_cluster

                    self.free_space = free_bytes // (bytes_per_sector * sectors_per_cluster)
                    self.total_space = total_bytes // (bytes_per_sector * sectors_per_cluster)
                else:
                    self.free_space = free_bytes // 512  # Fallback to default sector size
                    self.total_space = total_bytes // 512
            else:
                if self.controller.geometry:
                    geometry = self.controller.geometry
                    total_sectors = geometry.total_sectors
                    sectors_per_cluster = 1
                    if self.controller.boot_sector:
                        sectors_per_cluster = self.controller.boot_sector.sectors_per_cluster

                    if self.controller.boot_sector:
                        fs_info = self.controller.boot_sector
                        reserved = fs_info.reserved_sectors
                        fat_size = fs_info.sectors_per_fat * fs_info.num_fats
                        root_dir_sectors = (fs_info.root_entries * 32 + fs_info.bytes_per_sector - 1) // fs_info.bytes_per_sector
                        data_sectors = total_sectors - reserved - fat_size - root_dir_sectors
                        self.total_space = data_sectors // sectors_per_cluster
                    else:
                        self.total_space = (total_sectors - 33) // sectors_per_cluster  # 33 is typical overhead

                    self.free_space = self.total_space - len(self.busy_clusters)
        except Exception as e:
            self.busy_clusters = []
            self.free_space = 0
            self.total_space = 0
            print(f"Error getting busy clusters: {e}")

    def draw_disk_map(self):
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
