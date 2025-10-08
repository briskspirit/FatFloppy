# src/fatfloppy/gui/main_window.py
"""
Main window for the FatFloppy Disk Browser application.
Coordinates UI and delegates operations to specialized managers.
"""

import datetime
import logging
import os
from pathlib import Path
from typing import Any, Callable, Optional

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import (
    QAction,
    QFont,
    QFontDatabase,
    QIcon,
    QPalette,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDockWidget,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.controller import DiskController
from ..core.driver_factory import DriverFactory
from ..core.drivers import GREASEWEAZLE_AVAILABLE
from ..core.utils.logging_config import get_logger
from .disk_map import DiskMapView
from .file_browser import DragDropTreeWidget
from .managers import DiskManager, EditorManager, FileManager, SettingsManager
from .models import FileSystemNode
from .themes import get_dark_theme, get_light_theme

MAX_RECENT_FILES = 10
DEFAULT_DOCK_MIN_WIDTH = 150
DEFAULT_DOCK_MAX_WIDTH = 400
TREE_DOCK_MIN_WIDTH = 320
TREE_DOCK_MAX_WIDTH = 400
FILE_LIST_MIN_WIDTH = 400
DISK_MAP_MIN_WIDTH = 300
DEFAULT_FONT_SIZE = 12
TAB_STOP_SPACES = 8


class FileBrowserApp(QMainWindow):
    """
    Main application window for the FatFloppy Disk Browser.
    Coordinates UI and delegates operations to managers.
    """

    MAX_RECENT_FILES = MAX_RECENT_FILES
    logger: logging.Logger = get_logger(__name__)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """
        Initialize the main application window.

        Args:
            parent: The parent widget, defaults to None.
        """
        super().__init__(parent)

        self.root_node: Optional[FileSystemNode] = None
        self.current_node: Optional[FileSystemNode] = None
        self.current_path: str = "/"
        self.controller: Optional[DiskController] = None

        self.greaseweazle_available: bool = GREASEWEAZLE_AVAILABLE
        self.extension_to_driver_map: dict[str, str] = {}
        self.file_dialog_filter: str = ""
        self._build_file_dialog_filter()

        self.app_font: QFont
        self.toolbar: QToolBar
        self.tree_dock: QDockWidget
        self.tree_widget: QTreeWidget
        self.disk_info_dock: QDockWidget
        self.physical_format_group: QGroupBox
        self.physical_format_info: QLabel
        self.filesystem_group: QGroupBox
        self.filesystem_info: QLabel
        self.file_list_dock: QDockWidget
        self.file_list: DragDropTreeWidget
        self.disk_map_dock: QDockWidget
        self.disk_map: DiskMapView
        self.disk_map_view: QGraphicsView
        self.head_action: QAction
        self.text_viewer_dock: QDockWidget
        self.text_viewer: QPlainTextEdit
        self.save_button: QPushButton
        self.discard_button: QPushButton
        self.hex_viewer_dock: QDockWidget
        self.hex_viewer: QPlainTextEdit
        self.recent_files_menu: QMenu
        self.restore_layout_action: QAction

        self._init_ui()
        self._setup_fonts()
        self._update_theme()

        self.disk_manager = DiskManager(self)
        self.file_manager = FileManager(self)
        self.editor_manager = EditorManager(self)
        self.settings_manager = SettingsManager(self, self.MAX_RECENT_FILES)

        self._connect_manager_signals()
        self._load_window_state()

        QApplication.instance().styleHints().colorSchemeChanged.connect(
            self._update_theme
        )

        self.reset_ui()
        self.logger.info("FatFloppy application initialized.")

    @pyqtSlot()
    def add_file(self) -> None:
        """Delegates to FileManager."""
        self.file_manager.add_file()

    @pyqtSlot()
    def create_directory(self) -> None:
        """Delegates to FileManager."""
        self.file_manager.create_directory()

    @pyqtSlot()
    def create_disk_image(self) -> None:
        """Delegates to DiskManager."""
        self.disk_manager.create_disk_image()

    @pyqtSlot()
    def delete_selected_items(self) -> None:
        """Delegates to FileManager."""
        self.file_manager.delete_selected_items()

    @pyqtSlot()
    def discard_changes(self) -> None:
        """Delegates to EditorManager."""
        self.editor_manager.discard_changes()

    @pyqtSlot()
    def draw_disk_map(self) -> None:
        """Draws or redraws the disk map visualization."""
        if not self.controller:
            self.disk_map.scene.clear()
            self.disk_map._draw_no_disk_message(
                self.app_font, self.palette().color(QPalette.ColorRole.WindowText)
            )
            return

        text_color = self.palette().color(QPalette.ColorRole.WindowText)
        map_data = self.disk_manager.get_disk_map_data()

        self.disk_map.draw_disk_map(
            map_data["controller"],
            map_data["current_head"],
            map_data["busy_units"],
            map_data["free_space"],
            map_data["total_space"],
            self.app_font,
            text_color,
            selected_file_units=map_data["selected_file_units"],
            selected_file_path=map_data["selected_file_path"],
        )

        if (
            self.controller
            and self.controller.physical_format
            and self.controller.physical_format.heads > 1
        ):
            self.head_action.setText(
                f"Switch Head (Current: {self.disk_manager.current_head})"
            )

        self.logger.debug(f"Disk map drawn for head {self.disk_manager.current_head}.")

    @pyqtSlot()
    def extract_selected_items(self) -> None:
        """Delegates to FileManager."""
        self.file_manager.extract_selected_items()

    def import_multiple_paths(
        self, file_paths: list[str], target_path: str, auto_name: bool = True
    ) -> None:
        """
        Delegates to FileManager.

        Args:
            file_paths: List of local paths to import.
            target_path: Target path on disk image.
            auto_name: Whether to auto-generate names.
        """
        self.file_manager.import_multiple_paths(file_paths, target_path, auto_name)

    @pyqtSlot()
    def open_disk_image_file(self) -> None:
        """Delegates to DiskManager."""
        self.disk_manager.open_disk_image_file()

    @pyqtSlot()
    def open_physical_floppy(self) -> None:
        """Delegates to DiskManager."""
        self.disk_manager.open_physical_floppy()

    def refresh_filesystem_ui(self, preserve_path: Optional[str] = None) -> None:
        """
        Refreshes filesystem UI elements.

        Args:
            preserve_path: Path to navigate to after refresh, or None for root.
        """
        self.logger.info(f"Refreshing filesystem UI (preserve_path: {preserve_path}).")
        self.root_node = self._build_fs_tree()
        self._populate_tree_widget()

        if preserve_path:
            self._navigate_to_path(preserve_path)
        else:
            self.current_node = self.root_node
            self.current_path = "/"
            self.update_file_list()

        self.disk_manager.update_space_info()
        self.disk_manager.update_geometry_info()
        self.disk_manager.update_filesystem_info()
        self.draw_disk_map()
        self.statusBar().showMessage(f"Current path: {self.current_path}")
        self.logger.debug("Filesystem UI refresh complete.")

    def reset_ui(self) -> None:
        """Resets UI to initial no-disk-loaded state."""
        self.logger.info("Resetting UI to initial state.")

        self.root_node = None
        self.current_node = None
        self.current_path = "/"

        if self.controller:
            self.controller.close_disk()
            self.logger.debug("Closed existing disk controller.")
        self.controller = None

        self.disk_manager.current_head = 0
        self.disk_manager.busy_units = []
        self.disk_manager.free_space = 0
        self.disk_manager.total_space = 0
        self.disk_manager.clear_file_selection()

        self.tree_widget.clear()
        self.file_list.clear()
        self.physical_format_info.setText("No disk image loaded")
        self.filesystem_info.setText("No filesystem detected")
        self.disk_map.scene.clear()
        self.draw_disk_map()
        self.head_action.setEnabled(False)
        self.head_action.setText("Switch Head")
        self.statusBar().showMessage("Ready")

        self.editor_manager.clear_text_viewer_state()
        self.editor_manager.clear_hex_viewer_state()

        self.disk_map_dock.raise_()
        self.logger.debug("UI reset complete.")

    @pyqtSlot()
    def save_as_disk_image(self) -> None:
        """Delegates to DiskManager."""
        self.disk_manager.save_as_disk_image()

    @pyqtSlot()
    def save_file(self) -> bool:
        """
        Delegates to EditorManager.

        Returns:
            True if save succeeded or no changes, False otherwise.
        """
        return self.editor_manager.save_file()

    @pyqtSlot(QTreeWidgetItem, int)
    def select_directory(self, item: QTreeWidgetItem, _column: int) -> None:
        """
        Handles directory selection in the tree widget.

        Args:
            item: The selected QTreeWidgetItem.
            _column: The column index (unused but required by signal).
        """
        self.logger.debug(f"Directory '{item.text(0)}' selected in tree.")
        self.current_node = item.node
        self.current_path = self._build_path_from_node(item.node)
        self.update_file_list()
        self.statusBar().showMessage(f"Viewing: {self.current_path}")

    @pyqtSlot()
    def toggle_head(self) -> None:
        """Delegates to DiskManager."""
        self.disk_manager.toggle_head()

    def update_file_list(self) -> None:
        """Updates the file list widget based on current directory."""
        self.file_list.clear()
        if not self.current_node:
            self.logger.debug("No current node to update file list.")
            return

        for child in self.current_node.children:
            if child.name not in [".", ".."]:
                item = QTreeWidgetItem(
                    self.file_list,
                    [
                        child.name,
                        str(child.size) if not child.is_dir else "",
                        child.modified,
                        child.attributes,
                    ],
                )
                item.node = child
        self.logger.debug(f"File list updated for path: {self.current_path}")

    @pyqtSlot()
    def view_file_content(self) -> None:
        """Delegates to EditorManager."""
        self.editor_manager.view_file_content()

    def closeEvent(self, event) -> None:  # noqa: N802
        """
        Handles window close event.

        Args:
            event: The close event.
        """
        if self.editor_manager.has_unsaved_changes():
            reply = QMessageBox.question(
                self,
                "Unsaved Changes",
                f"Do you want to save changes to "
                f"{Path(self.editor_manager.current_file_path).name}?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )

            if reply == QMessageBox.StandardButton.Save:
                if not self.save_file():
                    event.ignore()
                    return
            elif reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return

        self.settings_manager.save_window_state()
        event.accept()

    def _build_file_dialog_filter(self) -> None:
        """
        Dynamically builds file dialog filter string from discovered drivers.
        """
        self.logger.debug("Building file dialog filter from discovered drivers.")
        self.extension_to_driver_map = DriverFactory.get_extension_map()

        all_extensions = sorted(self.extension_to_driver_map.keys())

        driver_to_exts: dict[str, list[str]] = {}
        for ext, driver_type in self.extension_to_driver_map.items():
            driver_to_exts.setdefault(driver_type, []).append(f"*{ext}")

        filters = []
        all_ext_str = " ".join(f"*{ext}" for ext in all_extensions)
        filters.append(f"All Supported Images ({all_ext_str})")

        for driver_type, exts in sorted(driver_to_exts.items()):
            exts_str = " ".join(exts)
            desc = getattr(
                DriverFactory.get_driver_class(driver_type),
                "driver_description",
                f"{driver_type} Files",
            )
            filters.append(f"{desc} ({exts_str})")

        filters.append("All Files (*)")
        self.file_dialog_filter = ";;".join(filters)
        self.logger.info(f"Generated file dialog filter: {self.file_dialog_filter}")

    def _build_fs_tree(self) -> Optional[FileSystemNode]:
        """
        Builds hierarchical representation of disk's filesystem.

        Returns:
            Root FileSystemNode of the tree, or None if no controller.
        """
        if not self.controller:
            return None

        self.logger.debug("Building filesystem tree.")
        root_node = FileSystemNode("Root", is_dir=True, attributes="-")
        node_dict: dict[str, FileSystemNode] = {"/": root_node}

        all_directories_to_scan: list[str] = ["/"]
        scanned_directories: list[str] = []
        directory_contents: dict[str, list[dict[str, Any]]] = {}

        while all_directories_to_scan:
            current_dir_path = all_directories_to_scan.pop(0)
            if current_dir_path in scanned_directories:
                continue
            scanned_directories.append(current_dir_path)

            try:
                items = self.controller.list_directory(current_dir_path)
                directory_contents[current_dir_path] = items
                for item in items:
                    if item["is_dir"] and item["name"] not in [".", ".."]:
                        next_dir_path = str(
                            (Path(current_dir_path) / item["name"]).resolve()
                        )
                        if (
                            next_dir_path not in scanned_directories
                            and next_dir_path not in all_directories_to_scan
                        ):
                            all_directories_to_scan.append(next_dir_path)
            except Exception as e:
                self.logger.error(
                    f"Error listing directory {current_dir_path} during tree build: {e}"
                )

        sorted_dir_paths = sorted(node_dict.keys(), key=lambda p: p.count("/"))
        for dir_path in sorted_dir_paths:
            if dir_path == "/":
                continue

            parts = dir_path.strip("/").split("/")
            parent_path = os.path.normpath("/" + "/".join(parts[:-1]))
            dir_name = parts[-1]

            parent_node = node_dict.get(parent_path)
            if not parent_node:
                self.logger.warning(
                    f"Parent node for '{dir_path}' not found. Skipping."
                )
                continue

            dir_info_found = None
            for item in directory_contents.get(parent_path, []):
                if item["is_dir"] and item["name"] == dir_name:
                    dir_info_found = item
                    break

            if not dir_info_found:
                self.logger.warning(
                    f"Directory info not found for '{dir_path}'. Skipping."
                )
                continue

            node = FileSystemNode(
                name=dir_name,
                size=0,
                is_dir=True,
                modified=(
                    dir_info_found["datetime"].strftime("%Y-%m-%d %H:%M:%S")
                    if isinstance(dir_info_found["datetime"], datetime.datetime)
                    else str(dir_info_found["datetime"])
                ),
                attributes=dir_info_found["attributes"],
                parent=parent_node,
            )
            parent_node.appendChild(node)
            node_dict[dir_path] = node

        for dir_path, items in directory_contents.items():
            parent_node = node_dict.get(dir_path)
            if not parent_node:
                continue

            for item in items:
                if not item["is_dir"]:
                    node = FileSystemNode(
                        name=item["name"],
                        size=item["size"],
                        is_dir=False,
                        modified=(
                            item["datetime"].strftime("%Y-%m-%d %H:%M:%S")
                            if isinstance(item["datetime"], datetime.datetime)
                            else str(item["datetime"])
                        ),
                        attributes=item["attributes"],
                        parent=parent_node,
                    )
                    parent_node.appendChild(node)

        self.logger.debug("Filesystem tree built successfully.")
        return root_node

    def _build_full_path(self, name: str) -> str:
        """
        Constructs full path from current path and item name.

        Args:
            name: Item name.

        Returns:
            Full path string.
        """
        path = self.current_path
        if path != "/":
            path += "/"
        return os.path.normpath(path + name)

    def _build_path_from_node(self, node: FileSystemNode) -> str:
        """
        Constructs full filesystem path from a FileSystemNode.

        Args:
            node: The FileSystemNode to get the path for.

        Returns:
            The full path string.
        """
        path_parts: list[str] = []
        curr = node
        while curr and curr.parent:
            path_parts.insert(0, curr.name)
            curr = curr.parent
        return "/" + "/".join(path_parts) if path_parts else "/"

    def _connect_manager_signals(self) -> None:
        """Connects manager signals to UI updates."""
        self.disk_manager.disk_opened.connect(self._on_disk_opened)
        self.disk_manager.disk_closed.connect(self.reset_ui)
        self.disk_manager.status_message.connect(
            lambda msg: self.statusBar().showMessage(msg)
        )
        self.disk_manager.error_occurred.connect(
            lambda title, msg: QMessageBox.critical(self, title, msg)
        )
        self.disk_manager.map_update_needed.connect(self.draw_disk_map)

        self.file_manager.operation_complete.connect(
            lambda msg: self.statusBar().showMessage(msg)
        )
        self.file_manager.refresh_needed.connect(self.refresh_filesystem_ui)
        self.file_manager.error_occurred.connect(
            lambda title, msg: QMessageBox.critical(self, title, msg)
        )

        self.editor_manager.refresh_needed.connect(self.refresh_filesystem_ui)
        self.editor_manager.error_occurred.connect(
            lambda title, msg: QMessageBox.critical(self, title, msg)
        )

        self.settings_manager.recent_files_changed.connect(
            lambda: self.settings_manager.update_recent_files_menu(
                self.recent_files_menu
            )
        )
        self.settings_manager.open_file_requested.connect(
            self.disk_manager.open_disk_image_by_path
        )

        self.text_viewer.textChanged.connect(self.editor_manager.on_text_editor_changed)

    def _connect_signals_slots(self) -> None:
        """Connects UI signals to slots."""
        self.file_list.selectionModel().selectionChanged.connect(
            self._on_file_list_selection_changed
        )
        self.statusBar().showMessage("Ready")

    def _create_disk_info_dock(self) -> None:
        """Creates disk information dock."""
        self.disk_info_dock = QDockWidget("Disk Information", self)
        self.disk_info_dock.setObjectName("DiskInfoDock")

        disk_info_widget = QWidget()
        disk_info_layout = QVBoxLayout(disk_info_widget)
        disk_info_layout.setContentsMargins(5, 5, 5, 5)
        disk_info_layout.setSpacing(6)

        self.physical_format_group = QGroupBox("Physical Geometry")
        self.physical_format_info = QLabel("No disk image loaded")
        self.physical_format_info.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.physical_format_info.setWordWrap(True)
        self.physical_format_info.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )

        geometry_layout = QVBoxLayout(self.physical_format_group)
        geometry_layout.addWidget(self.physical_format_info)
        self.physical_format_group.setLayout(geometry_layout)

        self.filesystem_group = QGroupBox("Filesystem")
        self.filesystem_info = QLabel("No filesystem detected")
        self.filesystem_info.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.filesystem_info.setWordWrap(True)
        self.filesystem_info.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )

        filesystem_layout = QVBoxLayout(self.filesystem_group)
        filesystem_layout.addWidget(self.filesystem_info)
        self.filesystem_group.setLayout(filesystem_layout)

        disk_info_layout.addWidget(self.physical_format_group)
        disk_info_layout.addWidget(self.filesystem_group)
        disk_info_layout.addStretch(1)

        disk_info_widget.setLayout(disk_info_layout)
        self.disk_info_dock.setWidget(disk_info_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.disk_info_dock)

    def _create_disk_map_dock(self) -> None:
        """Creates disk map dock."""
        self.disk_map_dock = QDockWidget("Disk Map", self)
        self.disk_map_dock.setObjectName("DiskMapDock")

        self.disk_map = DiskMapView(self)
        self.disk_map_view = self.disk_map.view

        disk_map_container = QWidget()
        disk_map_layout = QVBoxLayout(disk_map_container)
        disk_map_layout.setContentsMargins(0, 0, 0, 0)
        disk_map_layout.setSpacing(0)

        disk_map_toolbar = QToolBar("Disk Map Tools")
        disk_map_toolbar.setIconSize(self.toolbar.iconSize())
        disk_map_toolbar.setMovable(False)
        disk_map_toolbar.setStyleSheet("QToolBar { border: none; }")

        self.head_action = QAction("Switch Head", self)
        self.head_action.setToolTip("Switch between disk heads (sides)")
        self.head_action.triggered.connect(self.toggle_head)
        self.head_action.setEnabled(False)

        disk_map_toolbar.addAction(self.head_action)
        disk_map_layout.addWidget(disk_map_toolbar)
        disk_map_layout.addWidget(self.disk_map_view)

        self.disk_map_dock.setWidget(disk_map_container)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.disk_map_dock)

    def _create_docks(self) -> None:
        """Creates all dockable widgets."""
        self._create_tree_dock()
        self._create_disk_info_dock()
        self._create_file_list_dock()
        self._create_disk_map_dock()
        self._create_text_editor_dock()
        self._create_hex_viewer_dock()
        self.logger.debug("Docks created.")

    def _create_file_list_dock(self) -> None:
        """Creates file list dock."""
        self.file_list_dock = QDockWidget("Files", self)
        self.file_list_dock.setObjectName("FileListDock")

        self.file_list = DragDropTreeWidget(self)
        self.file_list.itemDoubleClicked.connect(self._on_file_double_clicked)
        self.file_list.setHeaderLabels(["Name", "Size", "Date/Time", "Attr"])
        self.file_list.setDragEnabled(True)
        self.file_list.setAcceptDrops(True)
        self.file_list.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.file_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )

        self.file_list_dock.setWidget(self.file_list)

        header = self.file_list.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)

        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.file_list_dock)

    def _create_hex_viewer_dock(self) -> None:
        """Creates hex viewer dock."""
        self.hex_viewer_dock = QDockWidget("Hex Viewer", self)
        self.hex_viewer_dock.setObjectName("HexViewerDock")

        hex_viewer_widget = QWidget()
        hex_viewer_layout = QVBoxLayout(hex_viewer_widget)
        hex_viewer_layout.setContentsMargins(2, 2, 2, 2)
        hex_viewer_layout.setSpacing(4)

        self.hex_viewer = QPlainTextEdit()
        self.hex_viewer.setReadOnly(True)
        self.hex_viewer.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        font_metrics = self.hex_viewer.fontMetrics()
        self.hex_viewer.setTabStopDistance(
            font_metrics.horizontalAdvance(" ") * TAB_STOP_SPACES
        )

        hex_viewer_layout.addWidget(self.hex_viewer)
        hex_viewer_widget.setLayout(hex_viewer_layout)
        self.hex_viewer_dock.setWidget(hex_viewer_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.hex_viewer_dock)

    def _create_menus(self) -> None:
        """Creates application menu bar."""
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")

        create_image_action = QAction("Create Disk Image", self)
        create_image_action.triggered.connect(self.create_disk_image)
        file_menu.addAction(create_image_action)

        open_image_action = QAction("Open Disk Image File", self)
        open_image_action.setToolTip("Open a disk image file (.ima, .img, .imd)")
        open_image_action.triggered.connect(self.open_disk_image_file)
        file_menu.addAction(open_image_action)

        self.recent_files_menu = QMenu("Recent Files", self)
        file_menu.addMenu(self.recent_files_menu)

        save_as_action = QAction("Save As...", self)
        save_as_action.setToolTip(
            "Save current disk image to a different format or location"
        )
        save_as_action.triggered.connect(self.save_as_disk_image)
        file_menu.addAction(save_as_action)

        file_menu.addSeparator()

        open_floppy_action = QAction("Open Physical Floppy", self)
        open_floppy_action.triggered.connect(self.open_physical_floppy)
        if not self.greaseweazle_available:
            open_floppy_action.setEnabled(False)
            self.logger.info(
                "Greaseweazle not available, 'Open Physical Floppy' disabled."
            )
        file_menu.addAction(open_floppy_action)

        file_menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.setMenuRole(QAction.MenuRole.QuitRole)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        view_menu = menu_bar.addMenu("&View")

        save_layout_action = QAction("Save Current Layout", self)
        save_layout_action.triggered.connect(self._save_current_layout)
        view_menu.addAction(save_layout_action)

        reset_layout_action = QAction("Reset to Default Layout", self)
        reset_layout_action.triggered.connect(self._reset_layout)
        view_menu.addAction(reset_layout_action)

        view_menu.addSeparator()

        from PyQt6.QtCore import QSettings

        settings = QSettings("FatFloppy", "FatFloppy")

        self.restore_layout_action = QAction("Restore Custom Layout on Startup", self)
        self.restore_layout_action.setCheckable(True)
        self.restore_layout_action.setChecked(
            settings.value("window/restore_dock_layout", False, type=bool)
        )
        self.restore_layout_action.triggered.connect(self._toggle_restore_layout)
        view_menu.addAction(self.restore_layout_action)

        self.logger.debug("Menus created.")

    def _create_text_editor_dock(self) -> None:
        """Creates text editor dock."""
        self.text_viewer_dock = QDockWidget("Text Editor", self)
        self.text_viewer_dock.setObjectName("TextEditorDock")

        text_viewer_widget = QWidget()
        text_viewer_layout = QVBoxLayout(text_viewer_widget)
        text_viewer_layout.setContentsMargins(2, 2, 2, 2)
        text_viewer_layout.setSpacing(4)

        self.text_viewer = QPlainTextEdit()
        self.text_viewer.setReadOnly(False)

        button_layout = QHBoxLayout()
        button_layout.setSpacing(6)

        self.save_button = QPushButton("Save Changes")
        self.save_button.clicked.connect(self.save_file)
        self.save_button.setEnabled(False)

        self.discard_button = QPushButton("Discard Changes")
        self.discard_button.clicked.connect(self.discard_changes)
        self.discard_button.setEnabled(False)

        button_layout.addStretch(0)
        button_layout.addWidget(self.discard_button)
        button_layout.addWidget(self.save_button)
        button_layout.addStretch(0)

        text_viewer_layout.addWidget(self.text_viewer)
        text_viewer_layout.addLayout(button_layout)

        text_viewer_widget.setLayout(text_viewer_layout)
        self.text_viewer_dock.setWidget(text_viewer_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.text_viewer_dock)

    def _create_toolbars(self) -> None:
        """Creates main toolbar."""
        self.toolbar = QToolBar("Main Toolbar", self)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.toolbar)

        create_image_action = QAction("Create Image", self)
        create_image_action.setToolTip("Create a new blank disk image")
        create_image_action.triggered.connect(self.create_disk_image)
        self.toolbar.addAction(create_image_action)

        open_image_action = QAction("Open Image", self)
        open_image_action.setToolTip("Open a disk image file")
        open_image_action.triggered.connect(self.open_disk_image_file)
        self.toolbar.addAction(open_image_action)

        open_floppy_action = QAction("Open Floppy", self)
        open_floppy_action.setToolTip("Open a physical floppy drive")
        open_floppy_action.triggered.connect(self.open_physical_floppy)
        if not self.greaseweazle_available:
            open_floppy_action.setEnabled(False)
        self.toolbar.addAction(open_floppy_action)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.toolbar.addWidget(spacer)

        view_action = QAction("View File", self)
        view_action.setToolTip("View selected file content")
        view_action.triggered.connect(self.view_file_content)
        self.toolbar.addAction(view_action)

        add_file_action = QAction("Add File", self)
        add_file_action.setToolTip("Add a file to current directory")
        add_file_action.triggered.connect(self.add_file)
        self.toolbar.addAction(add_file_action)

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

        self.logger.debug("Toolbars created.")

    def _create_tree_dock(self) -> None:
        """Creates directory tree dock."""
        self.tree_dock = QDockWidget("Directory Tree", self)
        self.tree_dock.setObjectName("TreeDock")
        self.tree_widget = QTreeWidget()
        self.tree_widget.setHeaderLabel("Directories")
        self.tree_widget.itemClicked.connect(self.select_directory)
        self.tree_dock.setWidget(self.tree_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.tree_dock)

    def _init_ui(self) -> None:
        """Initializes main UI components."""
        from PyQt6.QtCore import QSettings

        settings = QSettings("FatFloppy", "FatFloppy")
        restore_dock_layout = settings.value(
            "window/restore_dock_layout", False, type=bool
        )

        self._init_window_settings()
        self._create_menus()
        self._create_toolbars()
        self._create_docks()

        if not restore_dock_layout:
            self._setup_dock_layout()

        self._connect_signals_slots()
        self.logger.debug("UI components initialized.")

    def _init_window_settings(self) -> None:
        """Sets up initial window title and geometry."""
        self.setWindowTitle("FatFloppy Disk Browser")
        self.setGeometry(100, 100, 1200, 800)
        self.setDockNestingEnabled(True)

    def _load_window_state(self) -> None:
        """Loads and restores window geometry and dock positions."""
        self.settings_manager.load_window_state()

    def _navigate_to_path(self, path: str) -> bool:
        """
        Navigates file browser to specified path.

        Args:
            path: Target path string.

        Returns:
            True if navigation succeeded, False otherwise.
        """
        self.logger.debug(f"Navigating to path: {path}")
        if path == "/":
            self.current_node = self.root_node
            self.current_path = "/"
            self.update_file_list()
            self._select_tree_item_by_path(path)
            return True

        parts = path.strip("/").split("/")
        current = self.root_node

        for part in parts:
            found = False
            if current:
                for child in current.children:
                    if child.is_dir and child.name == part:
                        current = child
                        found = True
                        break
            if not found:
                self.logger.warning(f"Path part '{part}' not found. Resetting to root.")
                self.current_node = self.root_node
                self.current_path = "/"
                self.update_file_list()
                self._select_tree_item_by_path(self.current_path)
                return False

        self.current_node = current
        self.current_path = path
        self.update_file_list()
        self._select_tree_item_by_path(path)
        self.logger.info(f"Successfully navigated to path: {path}")
        return True

    def _on_disk_opened(self, source_identifier: str) -> None:
        """
        Handles disk opened signal from DiskManager.

        Args:
            source_identifier: Description of opened disk.
        """
        self.refresh_filesystem_ui()

        if self.controller and self.controller.physical_format:
            if self.controller.physical_format.heads > 1:
                self.head_action.setEnabled(True)
                self.head_action.setText(
                    f"Switch Head (Current: {self.disk_manager.current_head})"
                )
            else:
                self.head_action.setEnabled(False)
                self.head_action.setText("Single-sided disk")

        if Path(source_identifier).exists():
            self.settings_manager.add_to_recent_files(source_identifier)

    @pyqtSlot(QTreeWidgetItem, int)
    def _on_file_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        """
        Handles double-click on file list item.

        Args:
            item: The item that was double-clicked.
            _column: The column that was double-clicked.
        """
        if not hasattr(item, "node"):
            return

        node = item.node

        if node.is_dir:
            new_path = self._build_full_path(node.name)
            self._navigate_to_path(new_path)
            self.logger.debug(f"Navigated to directory: {new_path}")
        else:
            self.view_file_content()

    @pyqtSlot()
    def _on_file_list_selection_changed(self) -> None:
        """Handles changes in file list selection."""
        if self.editor_manager.has_unsaved_changes():
            reply = QMessageBox.warning(
                self,
                "Unsaved Changes",
                f"Do you want to save the changes to "
                f"{Path(self.editor_manager.current_file_path).name}?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )

            if reply == QMessageBox.StandardButton.Save:
                if not self.save_file():
                    self.logger.warning(
                        "Save during selection change failed/cancelled."
                    )
                    return
            elif reply == QMessageBox.StandardButton.Cancel:
                self.logger.debug("Selection change cancelled due to unsaved changes.")
                return

        self.disk_manager.clear_file_selection()
        self.editor_manager.clear_text_viewer_state()
        self.editor_manager.clear_hex_viewer_state()
        self.disk_map_dock.raise_()

        selected_items = self.file_list.selectedItems()

        if len(selected_items) == 1:
            item = selected_items[0]

            if not hasattr(item, "node"):
                self.logger.warning("Selected item does not have 'node' attribute.")
                return

            node = item.node

            if not node.is_dir:
                file_path = self._build_full_path(node.name)
                self.disk_manager.update_file_selection(file_path)

        self.draw_disk_map()

    def _populate_tree_widget(self) -> None:
        """Populates QTreeWidget from internal FileSystemNode tree."""
        self.tree_widget.clear()
        if not self.root_node:
            self.logger.debug("No root node to populate tree widget.")
            return

        root_item = QTreeWidgetItem(self.tree_widget, ["/"])
        root_item.node = self.root_node
        self._recursive_populate_tree_widget(self.root_node, root_item)
        self.tree_widget.expandAll()
        self.logger.debug("Tree widget populated.")

    def _recursive_populate_tree_widget(
        self, node: FileSystemNode, parent_item: QTreeWidgetItem
    ) -> None:
        """
        Recursively populates QTreeWidget with directory nodes.

        Args:
            node: Current FileSystemNode to process.
            parent_item: QTreeWidgetItem corresponding to parent node.
        """
        if not node:
            return

        for child in node.children:
            if child.is_dir:
                child_item = QTreeWidgetItem(parent_item, [child.name])
                child_item.node = child
                self._recursive_populate_tree_widget(child, child_item)

    def _reset_layout(self) -> None:
        """Wrapper to delegate to SettingsManager."""
        self.settings_manager.reset_layout()

    def _run_threaded_operation(
        self,
        operation: Callable,
        operation_name: str,
        on_success: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
        cancelable: bool = False,
        *args,
        **kwargs,
    ) -> None:
        """
        Runs a disk operation in background thread with progress dialog.

        Args:
            operation: Function to run in background.
            operation_name: Name for progress dialog.
            on_success: Callback for successful completion.
            on_error: Callback for errors.
            cancelable: Whether operation can be cancelled.
            *args, **kwargs: Arguments for the operation.
        """
        from .progress_dialog import ProgressDialog
        from .worker import DiskOperationWorker

        progress = ProgressDialog(
            title=operation_name,
            message=f"{operation_name}...",
            cancelable=cancelable,
            parent=self,
        )

        worker = DiskOperationWorker(operation, *args, **kwargs)
        worker.progress.connect(progress.update_progress)

        def on_finished(result):
            progress.accept()
            if on_success:
                on_success(result)

        def on_worker_error(exception):
            progress.reject()
            if on_error:
                on_error(exception)
            else:
                QMessageBox.critical(
                    self, "Error", f"{operation_name} failed: {str(exception)}"
                )
                self.logger.exception(f"Threaded operation '{operation_name}' failed")

        worker.finished.connect(on_finished)
        worker.error.connect(on_worker_error)

        if cancelable:
            progress.rejected.connect(worker.cancel)

        worker.start()
        progress.exec()
        worker.wait()

    def _save_current_layout(self) -> None:
        """Wrapper to delegate to SettingsManager."""
        self.settings_manager.save_current_layout()

    def _select_tree_item_by_path(self, path: str) -> None:
        """
        Selects corresponding item in directory tree widget for given path.

        Args:
            path: Path string to select.
        """
        if path == "/":
            if self.tree_widget.topLevelItemCount() > 0:
                self.tree_widget.setCurrentItem(self.tree_widget.topLevelItem(0))
            return

        parts = path.strip("/").split("/")
        if self.tree_widget.topLevelItemCount() == 0:
            return

        current_tree_item = self.tree_widget.topLevelItem(0)

        for part in parts:
            found = False
            for i in range(current_tree_item.childCount()):
                child_item = current_tree_item.child(i)
                if hasattr(child_item, "node") and child_item.node.name == part:
                    current_tree_item = child_item
                    found = True
                    break
            if not found:
                self.logger.warning(f"Tree item for path part '{part}' not found.")
                return

        self.tree_widget.setCurrentItem(current_tree_item)
        self.logger.debug(f"Tree item selected for path: {path}")

    def _setup_dock_layout(self) -> None:
        """Arranges dockable widgets."""
        self.splitDockWidget(
            self.tree_dock, self.disk_info_dock, Qt.Orientation.Vertical
        )
        self.splitDockWidget(
            self.file_list_dock, self.disk_map_dock, Qt.Orientation.Horizontal
        )

        self.tabifyDockWidget(self.disk_map_dock, self.text_viewer_dock)
        self.tabifyDockWidget(self.text_viewer_dock, self.hex_viewer_dock)
        self.disk_map_dock.raise_()

        self.resizeDocks(
            [self.tree_dock, self.file_list_dock],
            [200, 1000],
            Qt.Orientation.Horizontal,
        )
        self.resizeDocks(
            [self.file_list_dock, self.disk_map_dock],
            [400, 600],
            Qt.Orientation.Horizontal,
        )
        self.resizeDocks(
            [self.tree_dock, self.disk_info_dock], [400, 400], Qt.Orientation.Vertical
        )

        self.tree_dock.setMinimumWidth(TREE_DOCK_MIN_WIDTH)
        self.tree_dock.setMaximumWidth(TREE_DOCK_MAX_WIDTH)
        self.disk_info_dock.setMinimumWidth(DEFAULT_DOCK_MIN_WIDTH)
        self.disk_info_dock.setMaximumWidth(DEFAULT_DOCK_MAX_WIDTH)
        self.file_list_dock.setMinimumWidth(FILE_LIST_MIN_WIDTH)
        self.disk_map_dock.setMinimumWidth(DISK_MAP_MIN_WIDTH)

        from PyQt6.QtCore import QSettings

        settings = QSettings("FatFloppy", "FatFloppy")
        if not settings.contains("window/default_state"):
            settings.setValue("window/default_state", self.saveState())
            self.logger.debug("Saved default dock layout state")

        self.logger.debug("Dock layout configured.")

    def _setup_fonts(self) -> None:
        """Configures and sets fonts for UI elements."""
        custom_font_loaded = False

        current_dir = Path(__file__).resolve().parent
        root_dir = current_dir
        while not (root_dir / "assets").exists() and root_dir != root_dir.parent:
            root_dir = root_dir.parent

        font_path = root_dir / "assets" / "fonts" / "JetBrainsMono-Regular.ttf"

        self.logger.debug(f"Looking for font at: {font_path}")
        self.logger.debug(f"Font file exists: {font_path.exists()}")

        if font_path.exists():
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            self.logger.debug(f"Font ID returned: {font_id}")

            if font_id != -1:
                font_families = QFontDatabase.applicationFontFamilies(font_id)
                self.logger.debug(f"Font families available: {font_families}")

                if font_families:
                    family_name = font_families[0]
                    self.app_font = QFont(family_name)
                    self.app_font.setPointSize(DEFAULT_FONT_SIZE)
                    QApplication.instance().setFont(self.app_font)
                    custom_font_loaded = True
                    self.logger.info(f"Successfully loaded custom font: {family_name}")
                else:
                    self.logger.warning(
                        f"Font loaded but no families returned from {font_path}"
                    )
            else:
                self.logger.warning(f"Failed to load font from {font_path}")
        else:
            self.logger.warning(f"Custom font file not found at {font_path}")

        if not custom_font_loaded:
            monospace_fonts: list[str] = [
                "JetBrainsMono",
                "Courier New",
                "DejaVu Sans Mono",
                "Consolas",
                "Menlo",
                "Liberation Mono",
                "Monaco",
                "SF Mono",
            ]
            self.app_font = QFont()
            found_font = False

            for font_name in monospace_fonts:
                if QFontDatabase.hasFamily(font_name):
                    self.app_font.setFamily(font_name)
                    self.app_font.setPointSize(DEFAULT_FONT_SIZE)
                    found_font = True
                    self.logger.info(f"Using fallback monospace font: {font_name}")
                    break

            if not found_font:
                default_monospace = QFontDatabase.systemFont(
                    QFontDatabase.SystemFont.FixedFont
                )
                self.app_font = default_monospace
                self.app_font.setPointSize(DEFAULT_FONT_SIZE)
                self.logger.info(
                    f"Using system default monospace font: {default_monospace.family()}"
                )

            QApplication.instance().setFont(self.app_font)

        QApplication.instance().setFont(self.app_font)
        self.setFont(self.app_font)

        self.tree_widget.setFont(self.app_font)
        self.file_list.setFont(self.app_font)
        self.physical_format_info.setFont(self.app_font)
        self.filesystem_info.setFont(self.app_font)
        self.text_viewer.setFont(self.app_font)
        self.hex_viewer.setFont(self.app_font)
        self.physical_format_group.setFont(self.app_font)
        self.filesystem_group.setFont(self.app_font)
        self.tree_dock.setFont(self.app_font)
        self.disk_info_dock.setFont(self.app_font)
        self.file_list_dock.setFont(self.app_font)
        self.disk_map_dock.setFont(self.app_font)
        self.text_viewer_dock.setFont(self.app_font)
        self.hex_viewer_dock.setFont(self.app_font)
        self.toolbar.setFont(self.app_font)
        self.menuBar().setFont(self.app_font)
        self.statusBar().setFont(self.app_font)

        font_metrics = self.hex_viewer.fontMetrics()
        self.hex_viewer.setTabStopDistance(
            font_metrics.horizontalAdvance(" ") * TAB_STOP_SPACES
        )

        self.logger.info(
            f"Final font in use: {self.app_font.family()}, "
            f"Size: {self.app_font.pointSize()}"
        )

    def _toggle_restore_layout(self, checked: bool) -> None:
        """Wrapper to delegate to SettingsManager."""
        self.settings_manager.toggle_restore_layout(checked)

    @pyqtSlot()
    def _update_theme(self) -> None:
        """Updates application theme based on system color scheme."""
        color_scheme = QApplication.styleHints().colorScheme()
        if color_scheme == Qt.ColorScheme.Dark:
            style_sheet = get_dark_theme()
            self.logger.debug("Applying dark theme.")
        elif color_scheme == Qt.ColorScheme.Light:
            style_sheet = get_light_theme()
            self.logger.debug("Applying light theme.")
        else:
            style_sheet = get_dark_theme()
            self.logger.debug("Applying default dark theme.")
        self.setStyleSheet(style_sheet)


def run_gui() -> None:
    """Initializes and runs the FatFloppy GUI application."""
    import sys

    if sys.platform == "darwin":
        os.environ["RESOURCE_NAME"] = "FatFloppy"

    app = QApplication(sys.argv)

    app.setApplicationName("FatFloppy")
    app.setApplicationDisplayName("FatFloppy")
    app.setOrganizationName("FatFloppy")
    app.setOrganizationDomain("fatfloppy.local")

    if sys.platform == "darwin":
        app.setDesktopFileName("FatFloppy")

    current_dir = Path(__file__).resolve().parent
    root_dir = current_dir
    while not (root_dir / "assets").exists() and root_dir != root_dir.parent:
        root_dir = root_dir.parent
    icon_path = root_dir / "assets" / "icons" / "fatfloppy_icon.png"

    if icon_path.exists():
        app_icon = QIcon(str(icon_path))
        app.setWindowIcon(app_icon)
    else:
        FileBrowserApp.logger.warning(f"Application icon not found at: {icon_path}")

    window = FileBrowserApp()
    window.show()

    window.settings_manager.update_recent_files_menu(window.recent_files_menu)

    sys.exit(app.exec())
