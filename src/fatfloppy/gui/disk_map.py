# src/fatfloppy/gui/disk_map.py
import logging
import math
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import QPointF, Qt, QTimer
from PyQt6.QtGui import (QBrush, QColor, QFont, QPainter, QPen, QPolygonF,
                         QResizeEvent)
from PyQt6.QtWidgets import (QGraphicsEllipseItem, QGraphicsLineItem,
                             QGraphicsPolygonItem, QGraphicsScene,
                             QGraphicsSimpleTextItem, QGraphicsView, QWidget)

logger = logging.getLogger(__name__)


class ResizableGraphicsView(QGraphicsView):
    def __init__(self, scene: QGraphicsScene, parent: Optional[QWidget] = None):
        super().__init__(scene, parent)
        self.app = parent
        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._delayed_redraw)
        self.setSceneRect(0, 0, self.width() - 2, self.height() - 2)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.setSceneRect(0, 0, self.width() - 2, self.height() - 2)
        # Debounce: only redraw after resize stops for 100ms
        self._resize_timer.start(100)

    def _delayed_redraw(self) -> None:
        if hasattr(self.app, 'draw_disk_map'):
            self.app.draw_disk_map()


class DiskMapView:
    """
    Manages the visualization of a floppy disk's physical layout.
    """

    def __init__(self, parent: QWidget) -> None:
        """
        Initializes the DiskMapView.

        Args:
            parent: The parent widget.
        """
        self.parent = parent
        self.scene = QGraphicsScene()
        self.view = ResizableGraphicsView(self.scene, parent)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    # ##################################################################
    # Public Methods
    # ##################################################################

    def draw_disk_map(
        self,
        controller: Any,
        current_head: int,
        busy_units: Any,
        free_space: int,
        total_space: int,
        app_font: QFont,
        text_color: QColor,
        selected_file_units: Optional[List[int]] = None,
        selected_file_path: Optional[str] = None
    ) -> None:
        """
        Draws the entire disk map visualization onto the scene.

        Args:
            controller: The main application controller holding disk state.
            current_head: The disk head currently being visualized.
            busy_units: Information about busy allocation units.
            free_space: The amount of free space on the disk.
            total_space: The total space on the disk.
            app_font: The font to use for text rendering.
            text_color: The color for text rendering.
            selected_file_units: Optional list of allocation units for a selected file.
            selected_file_path: Optional path of the selected file for tooltip display.
        """
        self.scene.clear()

        if not controller or not controller.disk or not controller.physical_format:
            self._draw_no_disk_message(app_font, text_color)
            return

        geometry = controller.physical_format
        if current_head >= geometry.heads:
            self._draw_message(f"Invalid head selected: {current_head}", app_font, text_color)
            return

        layout_info = self._get_layout_info(controller.filesystem)

        # Store selected file information for use in sector coloring and tooltips
        self._selected_units = set(selected_file_units) if selected_file_units else set()
        self._selected_file_path = selected_file_path

        # Add "Selected File" to legend if we have a selection
        if self._selected_units and layout_info.get('legend'):
            layout_info['legend'].insert(0, ("Selected File", "#FFD700"))

        self._draw_legend(layout_info, app_font, text_color)
        self._draw_stats(current_head, free_space, total_space, app_font, text_color)
        self._draw_sectors(geometry, current_head, layout_info)
        self._draw_physical_layout_note(app_font, text_color)

    # ##################################################################
    # Private Drawing Helper Methods
    # ##################################################################

    def _draw_legend(self, layout_info: Dict[str, Any], font: QFont, color: QColor) -> None:
        """Draws the color legend for the disk map."""
        legend_x, legend_y = 10, 10
        square_size, vertical_spacing = 10, 15
        legend_items = layout_info.get('legend', [])

        if not legend_items:
            self._draw_message("Filesystem layout unknown", font, color, pos=QPointF(legend_x, legend_y))
            return

        for i, (label, color_hex) in enumerate(legend_items):
            rect_y = legend_y + i * vertical_spacing
            rect_item = QGraphicsPolygonItem(
                QPolygonF([
                    QPointF(legend_x, rect_y),
                    QPointF(legend_x + square_size, rect_y),
                    QPointF(legend_x + square_size, rect_y + square_size),
                    QPointF(legend_x, rect_y + square_size)
                ])
            )
            rect_item.setBrush(QBrush(QColor(color_hex)))
            rect_item.setPen(QPen(Qt.GlobalColor.black, 0.5))
            self.scene.addItem(rect_item)

            text_item = QGraphicsSimpleTextItem(label)
            text_item.setFont(font)
            text_item.setBrush(QBrush(color))
            text_rect = text_item.boundingRect()
            text_y_offset = (square_size - text_rect.height()) / 2
            text_item.setPos(legend_x + square_size + 5, rect_y + text_y_offset)
            self.scene.addItem(text_item)

    def _draw_message(self, message: str, font: QFont, color: QColor, pos: Optional[QPointF] = None) -> None:
        """Draws a simple text message on the scene."""
        text_item = QGraphicsSimpleTextItem(message)
        text_item.setFont(font)
        text_item.setBrush(QBrush(color))
        if pos:
            text_item.setPos(pos)
        else:
            text_rect = text_item.boundingRect()
            center_x = (self.view.width() - text_rect.width()) / 2
            center_y = (self.view.height() - text_rect.height()) / 2
            text_item.setPos(center_x, center_y)
        self.scene.addItem(text_item)

    def _draw_physical_layout_note(self, font: QFont, color: QColor) -> None:
        """Draws a note explaining this is the physical disk layout."""
        note_x = self.view.width() - 10
        note_y = self.view.height() - 15

        note_text = "Physical disk layout (sectors may appear non-sequential due to interleaving)"
        text_item = QGraphicsSimpleTextItem(note_text)

        # Use a smaller font for the note
        note_font = QFont(font)
        note_font.setPointSize(max(8, font.pointSize() - 2))
        note_font.setItalic(True)
        text_item.setFont(note_font)

        # Make it slightly transparent/gray
        note_color = QColor(color)
        note_color.setAlpha(180)
        text_item.setBrush(QBrush(note_color))

        # Position at bottom right
        text_rect = text_item.boundingRect()
        text_item.setPos(note_x - text_rect.width(), note_y)

        self.scene.addItem(text_item)

    def _draw_no_disk_message(self, font: QFont, color: QColor) -> None:
        """Displays a message when no disk is loaded."""
        self._draw_message("No disk loaded or geometry unknown", font, color)

    def _draw_sectors(self, geometry: Any, current_head: int, layout_info: Dict[str, Any]) -> None:
        """Draws the cylinders and sectors of the disk."""
        view_width, view_height = self.view.width(), self.view.height()
        x0, y0 = view_width / 2, view_height / 2
        r_min = min(view_width, view_height) * 0.1
        r_max = min(view_width, view_height) * 0.45
        num_cylinders = max(1, geometry.cylinders)
        use_lba_for_color = not geometry.has_variable_bps

        for c in range(num_cylinders):
            try:
                sectors_per_track = geometry.get_sectors_per_track(c, current_head)
            except ValueError:
                continue
            if sectors_per_track <= 0:
                continue

            angle_per_sector_deg = 360.0 / sectors_per_track
            r_outer = r_max - (r_max - r_min) * c / num_cylinders
            r_inner = r_max - (r_max - r_min) * (c + 1) / num_cylinders

            for i in range(sectors_per_track):
                self._draw_single_sector(
                    c, current_head, i, sectors_per_track, angle_per_sector_deg,
                    r_inner, r_outer, x0, y0, geometry, layout_info, use_lba_for_color
                )

            # Draw radial lines separating sectors
            for sector_idx in range(sectors_per_track):
                theta = math.radians(sector_idx * angle_per_sector_deg)
                p1 = QPointF(x0 + r_inner * math.cos(theta), y0 + r_inner * math.sin(theta))
                p2 = QPointF(x0 + r_outer * math.cos(theta), y0 + r_outer * math.sin(theta))
                line = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
                line.setPen(QPen(Qt.GlobalColor.black, 0.5))
                self.scene.addItem(line)

        # Draw concentric circles for cylinders
        for c_idx in range(1, num_cylinders):
            r = r_max - (r_max - r_min) * c_idx / num_cylinders
            ellipse = QGraphicsEllipseItem(x0 - r, y0 - r, 2 * r, 2 * r)
            ellipse.setPen(QPen(Qt.GlobalColor.black, 0.5))
            ellipse.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            self.scene.addItem(ellipse)

        # Draw the index hole
        if num_cylinders > 0:
            r_inner_final = r_max - (r_max - r_min)
            index_hole_radius = 5
            index_hole_x = x0 + r_inner_final - index_hole_radius
            index_hole_y = y0 - index_hole_radius
            index_hole = QGraphicsEllipseItem(index_hole_x, index_hole_y, 2 * index_hole_radius, 2 * index_hole_radius)
            index_hole.setBrush(QBrush(QColor("#008B8B")))  # Dark Cyan
            index_hole.setPen(QPen(Qt.GlobalColor.black, 0.5))
            self.scene.addItem(index_hole)

    def _draw_single_sector(
        self, c: int, h: int, i: int, sectors_per_track: int, angle_per_sector_deg: float,
        r_inner: float, r_outer: float, x0: float, y0: float, geometry: Any,
        layout_info: Dict[str, Any], use_lba_for_color: bool
    ) -> None:
        """Draws a single sector polygon with color and tooltip."""
        lba = -1
        sector_num = i + 1  # Assuming sectors are 1-based for CHS

        try:
            if use_lba_for_color:
                lba = geometry.chs_to_lba(c, h, sector_num)

            color_hex = self._get_sector_color(lba, c, h, sector_num, layout_info, use_lba_for_color)
            color = QColor(color_hex)

            theta_start = math.radians(i * angle_per_sector_deg)
            theta_end = math.radians((i + 1) * angle_per_sector_deg)

            inner_points = DiskMapView.generate_arc_points(x0, y0, r_inner, theta_start, theta_end, 20)
            outer_points = DiskMapView.generate_arc_points(x0, y0, r_outer, theta_end, theta_start, 20)

            polygon = QGraphicsPolygonItem(QPolygonF(inner_points + outer_points))
            polygon.setBrush(QBrush(color))
            polygon.setPen(QPen(Qt.GlobalColor.black, 0.5))

            # Create tooltip
            tooltip = self._create_sector_tooltip(lba, c, h, sector_num, layout_info, use_lba_for_color)
            polygon.setToolTip(tooltip)

            self.scene.addItem(polygon)

        except ValueError as e:
            app_logger = self._get_app_logger()
            log_msg = f"Error getting LBA for C:{c} H:{h} S:{sector_num}: {e}"
            if app_logger:
                app_logger.error(log_msg)
            else:
                logger.error(log_msg)
        except Exception as e:
            app_logger = self._get_app_logger()
            log_msg = f"Error drawing sector C:{c} H:{h} S:{sector_num}: {e}"
            if app_logger:
                app_logger.error(log_msg, exc_info=True)
            else:
                logger.error(log_msg, exc_info=True)

    def _create_sector_tooltip(
        self,
        lba: int,
        cylinder: int,
        head: int,
        sector: int,
        layout_info: Optional[Dict[str, Any]],
        use_lba: bool
    ) -> str:
        """
        Creates a tooltip string for a sector showing its location and purpose.

        Args:
            lba: The logical block address of the sector.
            cylinder, head, sector: The CHS address of the sector.
            layout_info: Dictionary containing filesystem layout details.
            use_lba: Flag indicating if LBA addressing is used.

        Returns:
            A formatted tooltip string.
        """
        tooltip_parts = [f"C:{cylinder} H:{head} S:{sector}"]

        if lba >= 0:
            tooltip_parts.append(f"LBA: {lba}")

        # Determine sector type and allocation unit
        allocation_unit = None
        sector_type_desc = "Unknown"

        if layout_info and lba >= 0:
            allocation_unit_size = layout_info.get('allocation_unit_size_sectors', 1)
            first_data_sector = layout_info.get('first_data_sector', 0)

            # Get sector type
            if use_lba:
                get_type_func = layout_info.get('get_sector_type')
                if get_type_func:
                    try:
                        sector_type = get_type_func(lba)
                        type_color_map = layout_info.get('type_color_map', {})
                        # Reverse lookup the type description from color map
                        for desc, color in layout_info.get('legend', []):
                            if type_color_map.get(sector_type) == color:
                                sector_type_desc = desc
                                break
                    except Exception:
                        pass
            else:
                get_type_chs_func = layout_info.get('get_sector_type_chs')
                if get_type_chs_func:
                    try:
                        sector_type = get_type_chs_func(cylinder, head, sector)
                        type_color_map = layout_info.get('type_color_map', {})
                        for desc, color in layout_info.get('legend', []):
                            if type_color_map.get(sector_type) == color:
                                sector_type_desc = desc
                                break
                    except Exception:
                        pass

            # Calculate allocation unit number if in data area
            if lba >= first_data_sector and allocation_unit_size > 0:
                allocation_unit = (lba - first_data_sector) // allocation_unit_size

        tooltip_parts.append(f"Type: {sector_type_desc}")

        if allocation_unit is not None:
            tooltip_parts.append(f"Unit: {allocation_unit}")

            # Check if this unit belongs to the selected file
            if hasattr(self, '_selected_units') and allocation_unit in self._selected_units:
                if hasattr(self, '_selected_file_path') and self._selected_file_path:
                    filename = self._selected_file_path.split('/')[-1]
                    tooltip_parts.append(f"★ Selected File: {filename}")

        return "\n".join(tooltip_parts)

    def _draw_stats(self, current_head: int, free_space: int, total_space: int, font: QFont, color: QColor) -> None:
        """Draws the disk space statistics text."""
        stats_x = 10
        stats_y = self.view.height() - 80
        unit_name = "units"
        stats_content = (
            f"Head: {current_head}\n"
            f"Total: {total_space} {unit_name}\n"
            f"Free: {free_space} {unit_name}\n"
            f"Used: {total_space - free_space} {unit_name}"
        )
        self._draw_message(stats_content, font, color, pos=QPointF(stats_x, stats_y))

    # ##################################################################
    # Private Helper Methods
    # ##################################################################

    def _get_app_logger(self) -> Optional[logging.Logger]:
        """Safely retrieves the logger from the parent's controller."""
        try:
            return self.parent.controller.logger
        except AttributeError:
            return None

    def _get_layout_info(self, filesystem: Optional[Any]) -> Dict[str, Any]:
        """
        Retrieves disk layout information from the filesystem object or
        returns a default structure if unavailable.
        """
        if filesystem and hasattr(filesystem, 'get_disk_map_layout'):
            return filesystem.get_disk_map_layout()
        return {
            'legend': [("Unknown/Data", "#BEBEBE")],
            'get_sector_type': lambda lba: "unknown",
            'allocation_unit_size_sectors': 1,
            'first_data_sector': 0,
            'type_color_map': {"unknown": "#BEBEBE"}
        }

    def _get_sector_color(
        self,
        lba: int,
        cylinder: int,
        head: int,
        sector: int,
        layout_info: Optional[Dict[str, Any]],
        use_lba: bool
    ) -> str:
        """
        Determines the color hex string for a sector based on its type.

        Args:
            lba: The logical block address of the sector.
            cylinder, head, sector: The CHS address of the sector.
            layout_info: Dictionary containing filesystem layout details.
            use_lba: Flag to determine if LBA or CHS should be used for lookup.

        Returns:
            The hex color code for the sector.
        """
        default_color = "#BEBEBE"
        error_color = "#8B0000"
        selected_file_color = "#00FF00"  # Gold for selected file

        if not layout_info:
            return default_color

        type_color_map: Dict[str, str] = layout_info.get('type_color_map', {})
        sector_type = "unknown"

        try:
            # First, check if this sector belongs to the selected file
            if hasattr(self, '_selected_units') and self._selected_units and lba >= 0:
                allocation_unit_size = layout_info.get('allocation_unit_size_sectors', 1)
                first_data_sector = layout_info.get('first_data_sector', 0)

                if lba >= first_data_sector and allocation_unit_size > 0:
                    allocation_unit = (lba - first_data_sector) // allocation_unit_size

                    if allocation_unit in self._selected_units:
                        return selected_file_color

            # Otherwise, use normal sector type coloring
            if use_lba:
                get_type_func: Optional[Callable[[int], str]] = layout_info.get('get_sector_type')
                if get_type_func:
                    sector_type = get_type_func(lba)
            else:
                get_type_chs_func: Optional[Callable[[int, int, int], str]] = layout_info.get('get_sector_type_chs')
                if get_type_chs_func:
                    sector_type = get_type_chs_func(cylinder, head, sector)

            return type_color_map.get(sector_type, default_color)

        except Exception as e:
            app_logger = self._get_app_logger()
            log_msg = f"Error getting sector color for LBA {lba} / CHS {cylinder},{head},{sector}: {e}"
            if app_logger:
                app_logger.error(log_msg)
            else:
                logger.error(log_msg)
            return error_color

    # ##################################################################
    # Static Methods
    # ##################################################################

    @staticmethod
    def generate_arc_points(
        x0: float,
        y0: float,
        radius: float,
        theta_start: float,
        theta_end: float,
        num_points: int
    ) -> List[QPointF]:
        """
        Generates a list of points along an arc.

        Args:
            x0, y0: The center coordinates of the arc.
            radius: The radius of the arc.
            theta_start, theta_end: The start and end angles in radians.
            num_points: The number of points to generate for the arc.

        Returns:
            A list of QPointF objects representing the arc.
        """
        points = []
        num_points = max(2, num_points)
        delta_theta = (theta_end - theta_start) / (num_points - 1)
        for i in range(num_points):
            theta = theta_start + i * delta_theta
            x = x0 + radius * math.cos(theta)
            y = y0 + radius * math.sin(theta)
            points.append(QPointF(x, y))
        return points
