# src/fatfloppy/gui/disk_map.py
import contextlib
import logging
import math
from typing import Any, Callable, Optional

from PyQt6.QtCore import QPointF, Qt, QTimer
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF, QResizeEvent
from PyQt6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsPolygonItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QWidget,
)

logger = logging.getLogger(__name__)

RESIZE_DEBOUNCE_MS = 100
LEGEND_X = 10
LEGEND_Y = 10
LEGEND_SQUARE_SIZE = 10
LEGEND_VERTICAL_SPACING = 15
LEGEND_TEXT_OFFSET = 5
STATS_X = 10
DISK_RADIUS_INNER_MULTIPLIER = 0.1
DISK_RADIUS_OUTER_MULTIPLIER = 0.45
INDEX_HOLE_RADIUS = 5
ARC_POINTS_COUNT = 20
DEFAULT_COLOR = "#BEBEBE"
ERROR_COLOR = "#8B0000"
SELECTED_FILE_COLOR = "#00FF00"
INDEX_HOLE_COLOR = "#008B8B"


class ResizableGraphicsView(QGraphicsView):
    """Graphics view that debounces resize events to improve performance."""

    def __init__(self, scene: QGraphicsScene, parent: Optional[QWidget] = None):
        """
        Initialize the resizable graphics view.

        Args:
            scene: The graphics scene to display.
            parent: The parent widget.
        """
        super().__init__(scene, parent)
        self.app = parent
        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._delayed_redraw)
        self.setSceneRect(0, 0, self.width() - 2, self.height() - 2)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        """
        Handle resize events with debouncing.

        Args:
            event: The resize event.
        """
        super().resizeEvent(event)
        self.setSceneRect(0, 0, self.width() - 2, self.height() - 2)
        self._resize_timer.start(RESIZE_DEBOUNCE_MS)

    def _delayed_redraw(self) -> None:
        """Trigger a delayed redraw after resize completes."""
        if hasattr(self.app, "draw_disk_map"):
            self.app.draw_disk_map()


class DiskMapView:
    """
    Manages the visualization of a floppy disk's logical layout.

    Sectors are drawn sequentially (0, 1, 2, ...) around the disk,
    representing the logical block addressing scheme.
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

    def draw_disk_map(
        self,
        controller: Any,
        current_head: int,
        _busy_units: Any,
        free_space: int,
        total_space: int,
        app_font: QFont,
        text_color: QColor,
        selected_file_units: Optional[list[int]] = None,
        selected_file_path: Optional[str] = None,
    ) -> None:
        """
        Draws the entire disk map visualization onto the scene.

        Args:
            controller: The main application controller holding disk state.
            current_head: The disk head currently being visualized.
            _busy_units: Information about busy allocation units (unused).
            free_space: The amount of free space on the disk.
            total_space: The total space on the disk.
            app_font: The font to use for text rendering.
            text_color: The color for text rendering.
            selected_file_units: Optional list of allocation units for selected file.
            selected_file_path: Optional path of the selected file for tooltip.
        """
        self.scene.clear()

        if not controller or not controller.disk or not controller.physical_format:
            self._draw_no_disk_message(app_font, text_color)
            return

        geometry = controller.physical_format
        if current_head >= geometry.heads:
            self._draw_message(
                f"Invalid head selected: {current_head}", app_font, text_color
            )
            return

        layout_info = self._get_layout_info(controller.filesystem)

        self._selected_units = (
            set(selected_file_units) if selected_file_units else set()
        )
        self._selected_file_path = selected_file_path

        if self._selected_units and layout_info.get("legend"):
            layout_info["legend"].insert(0, ("Selected File", SELECTED_FILE_COLOR))

        self._draw_legend(layout_info, app_font, text_color)
        self._draw_stats(current_head, free_space, total_space, app_font, text_color)
        self._draw_sectors(geometry, current_head, layout_info)
        self._draw_logical_layout_note(app_font, text_color)

    @staticmethod
    def generate_arc_points(
        x0: float,
        y0: float,
        radius: float,
        theta_start: float,
        theta_end: float,
        num_points: int,
    ) -> list[QPointF]:
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

    def _create_sector_tooltip(
        self,
        lba: int,
        cylinder: int,
        head: int,
        physical_sector_id: int,
        layout_info: Optional[dict[str, Any]],
    ) -> str:
        """
        Creates a tooltip string for a sector showing its location and purpose.

        Args:
            lba: The logical block address of the sector.
            cylinder, head: The CH address of the sector.
            physical_sector_id: The physical sector ID on the track.
            layout_info: Dictionary containing filesystem layout details.

        Returns:
            A formatted tooltip string.
        """
        tooltip_parts = [
            f"LBA: {lba}",
            f"C:{cylinder} H:{head} PhysSec:{physical_sector_id}",
        ]

        allocation_unit = None
        sector_type_desc = "Unknown"

        if layout_info and lba >= 0:
            allocation_unit_size = layout_info.get("allocation_unit_size_sectors", 1)
            first_data_sector = layout_info.get("first_data_sector", 0)

            get_type_func = layout_info.get("get_sector_type")
            if get_type_func:
                try:
                    sector_type = get_type_func(lba)
                    type_color_map = layout_info.get("type_color_map", {})
                    for desc, color in layout_info.get("legend", []):
                        if type_color_map.get(sector_type) == color:
                            sector_type_desc = desc
                            break
                except Exception:
                    pass

            if lba >= first_data_sector and allocation_unit_size > 0:
                allocation_unit = (lba - first_data_sector) // allocation_unit_size

        tooltip_parts.append(f"Type: {sector_type_desc}")

        if allocation_unit is not None:
            tooltip_parts.append(f"Unit: {allocation_unit}")

            if (
                hasattr(self, "_selected_units")
                and allocation_unit in self._selected_units
                and hasattr(self, "_selected_file_path")
                and self._selected_file_path
            ):
                filename = self._selected_file_path.split("/")[-1]
                tooltip_parts.append(f"★ Selected File: {filename}")

        return "\n".join(tooltip_parts)

    def _draw_legend(
        self, layout_info: dict[str, Any], font: QFont, color: QColor
    ) -> None:
        """
        Draws the color legend for the disk map.

        Args:
            layout_info: Dictionary containing legend information.
            font: Font to use for legend text.
            color: Color to use for legend text.
        """
        legend_items = layout_info.get("legend", [])

        if not legend_items:
            self._draw_message(
                "Filesystem layout unknown",
                font,
                color,
                pos=QPointF(LEGEND_X, LEGEND_Y),
            )
            return

        for i, (label, color_hex) in enumerate(legend_items):
            rect_y = LEGEND_Y + i * LEGEND_VERTICAL_SPACING
            rect_item = QGraphicsPolygonItem(
                QPolygonF(
                    [
                        QPointF(LEGEND_X, rect_y),
                        QPointF(LEGEND_X + LEGEND_SQUARE_SIZE, rect_y),
                        QPointF(
                            LEGEND_X + LEGEND_SQUARE_SIZE, rect_y + LEGEND_SQUARE_SIZE
                        ),
                        QPointF(LEGEND_X, rect_y + LEGEND_SQUARE_SIZE),
                    ]
                )
            )
            rect_item.setBrush(QBrush(QColor(color_hex)))
            rect_item.setPen(QPen(Qt.GlobalColor.black, 0.5))
            self.scene.addItem(rect_item)

            text_item = QGraphicsSimpleTextItem(label)
            text_item.setFont(font)
            text_item.setBrush(QBrush(color))
            text_rect = text_item.boundingRect()
            text_y_offset = (LEGEND_SQUARE_SIZE - text_rect.height()) / 2
            text_item.setPos(
                LEGEND_X + LEGEND_SQUARE_SIZE + LEGEND_TEXT_OFFSET,
                rect_y + text_y_offset,
            )
            self.scene.addItem(text_item)

    def _draw_logical_layout_note(self, font: QFont, color: QColor) -> None:
        """
        Draws a note explaining this is the logical disk layout.

        Args:
            font: Font to use for the note.
            color: Color to use for the note.
        """
        note_x = self.view.width() - 10
        note_y = self.view.height() - 15

        note_text = "Logical disk layout (sectors numbered sequentially 0, 1, 2, ...)"
        text_item = QGraphicsSimpleTextItem(note_text)

        note_font = QFont(font)
        note_font.setPointSize(max(8, font.pointSize() - 2))
        note_font.setItalic(True)
        text_item.setFont(note_font)

        note_color = QColor(color)
        note_color.setAlpha(180)
        text_item.setBrush(QBrush(note_color))

        text_rect = text_item.boundingRect()
        text_item.setPos(note_x - text_rect.width(), note_y)

        self.scene.addItem(text_item)

    def _draw_message(
        self, message: str, font: QFont, color: QColor, pos: Optional[QPointF] = None
    ) -> None:
        """
        Draws a simple text message on the scene.

        Args:
            message: The message to display.
            font: Font to use for the message.
            color: Color to use for the message.
            pos: Optional position for the message.
        """
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

    def _draw_no_disk_message(self, font: QFont, color: QColor) -> None:
        """
        Displays a message when no disk is loaded.

        Args:
            font: Font to use for the message.
            color: Color to use for the message.
        """
        self._draw_message("No disk loaded or geometry unknown", font, color)

    def _draw_sectors(
        self, geometry: Any, current_head: int, layout_info: dict[str, Any]
    ) -> None:
        """
        Draws the cylinders and sectors of the disk in logical order.

        Args:
            geometry: The physical format object.
            current_head: The current head being visualized.
            layout_info: Dictionary containing layout information.
        """
        view_width, view_height = self.view.width(), self.view.height()
        x0, y0 = view_width / 2, view_height / 2
        r_min = min(view_width, view_height) * DISK_RADIUS_INNER_MULTIPLIER
        r_max = min(view_width, view_height) * DISK_RADIUS_OUTER_MULTIPLIER
        num_cylinders = max(1, geometry.cylinders)

        starting_lba = 0
        for c in range(num_cylinders):
            for h in range(current_head):
                with contextlib.suppress(ValueError):
                    starting_lba += geometry.get_sectors_per_track(c, h)

        current_lba = starting_lba

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
                    c,
                    current_head,
                    i,
                    current_lba,
                    sectors_per_track,
                    angle_per_sector_deg,
                    r_inner,
                    r_outer,
                    x0,
                    y0,
                    geometry,
                    layout_info,
                )
                current_lba += 1

            for sector_idx in range(sectors_per_track):
                theta = math.radians(sector_idx * angle_per_sector_deg)
                p1 = QPointF(
                    x0 + r_inner * math.cos(theta), y0 + r_inner * math.sin(theta)
                )
                p2 = QPointF(
                    x0 + r_outer * math.cos(theta), y0 + r_outer * math.sin(theta)
                )
                line = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
                line.setPen(QPen(Qt.GlobalColor.black, 0.5))
                self.scene.addItem(line)

        for c_idx in range(1, num_cylinders):
            r = r_max - (r_max - r_min) * c_idx / num_cylinders
            ellipse = QGraphicsEllipseItem(x0 - r, y0 - r, 2 * r, 2 * r)
            ellipse.setPen(QPen(Qt.GlobalColor.black, 0.5))
            ellipse.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            self.scene.addItem(ellipse)

        if num_cylinders > 0:
            r_inner_final = r_max - (r_max - r_min)
            index_hole_x = x0 + r_inner_final - INDEX_HOLE_RADIUS
            index_hole_y = y0 - INDEX_HOLE_RADIUS
            index_hole = QGraphicsEllipseItem(
                index_hole_x, index_hole_y, 2 * INDEX_HOLE_RADIUS, 2 * INDEX_HOLE_RADIUS
            )
            index_hole.setBrush(QBrush(QColor(INDEX_HOLE_COLOR)))
            index_hole.setPen(QPen(Qt.GlobalColor.black, 0.5))
            self.scene.addItem(index_hole)

    def _draw_single_sector(
        self,
        c: int,
        h: int,
        logical_index: int,
        lba: int,
        _sectors_per_track: int,
        angle_per_sector_deg: float,
        r_inner: float,
        r_outer: float,
        x0: float,
        y0: float,
        geometry: Any,
        layout_info: dict[str, Any],
    ) -> None:
        """
        Draws a single sector polygon with color and tooltip.

        Args:
            c: Cylinder number
            h: Head number
            logical_index: Logical position on this track (0-based)
            lba: The logical block address of this sector
            _sectors_per_track: Number of sectors on this track (unused)
            angle_per_sector_deg: Angle per sector in degrees
            r_inner, r_outer: Inner and outer radii
            x0, y0: Center coordinates
            geometry: Physical format object
            layout_info: Layout information dictionary
        """
        try:
            track_format = geometry.get_track_format(c, h)
            physical_sector_id = track_format.logical_to_physical_sector(logical_index)

            color_hex = self._get_sector_color(lba, layout_info)
            color = QColor(color_hex)

            theta_start = math.radians(logical_index * angle_per_sector_deg)
            theta_end = math.radians((logical_index + 1) * angle_per_sector_deg)

            inner_points = DiskMapView.generate_arc_points(
                x0, y0, r_inner, theta_start, theta_end, ARC_POINTS_COUNT
            )
            outer_points = DiskMapView.generate_arc_points(
                x0, y0, r_outer, theta_end, theta_start, ARC_POINTS_COUNT
            )

            polygon = QGraphicsPolygonItem(QPolygonF(inner_points + outer_points))
            polygon.setBrush(QBrush(color))
            polygon.setPen(QPen(Qt.GlobalColor.black, 0.5))

            tooltip = self._create_sector_tooltip(
                lba, c, h, physical_sector_id, layout_info
            )
            polygon.setToolTip(tooltip)

            self.scene.addItem(polygon)

        except Exception as e:
            app_logger = self._get_app_logger()
            log_msg = (
                f"Error drawing sector C:{c} H:{h} logical:{logical_index} "
                f"LBA:{lba}: {e}"
            )
            if app_logger:
                app_logger.error(log_msg, exc_info=True)
            else:
                logger.error(log_msg, exc_info=True)

    def _draw_stats(
        self,
        current_head: int,
        free_space: int,
        total_space: int,
        font: QFont,
        color: QColor,
    ) -> None:
        """
        Draws the disk space statistics text.

        Args:
            current_head: The current head number.
            free_space: The amount of free space.
            total_space: The total space.
            font: Font to use for the statistics.
            color: Color to use for the statistics.
        """
        stats_y = self.view.height() - 80
        unit_name = "units"
        stats_content = (
            f"Head: {current_head}\n"
            f"Total: {total_space} {unit_name}\n"
            f"Free: {free_space} {unit_name}\n"
            f"Used: {total_space - free_space} {unit_name}"
        )
        self._draw_message(stats_content, font, color, pos=QPointF(STATS_X, stats_y))

    def _get_app_logger(self) -> Optional[logging.Logger]:
        """
        Safely retrieves the logger from the parent's controller.

        Returns:
            The logger instance or None if unavailable.
        """
        try:
            return self.parent.controller.logger
        except AttributeError:
            return None

    def _get_layout_info(self, filesystem: Optional[Any]) -> dict[str, Any]:
        """
        Retrieves disk layout information from the filesystem object.

        Args:
            filesystem: The filesystem object.

        Returns:
            Dictionary containing layout information or default structure.
        """
        if filesystem and hasattr(filesystem, "get_disk_map_layout"):
            return filesystem.get_disk_map_layout()
        return {
            "legend": [("Unknown/Data", DEFAULT_COLOR)],
            "get_sector_type": lambda _lba: "unknown",
            "allocation_unit_size_sectors": 1,
            "first_data_sector": 0,
            "type_color_map": {"unknown": DEFAULT_COLOR},
        }

    def _get_sector_color(self, lba: int, layout_info: Optional[dict[str, Any]]) -> str:
        """
        Determines the color hex string for a sector based on its type and LBA.

        Args:
            lba: The logical block address of the sector.
            layout_info: Dictionary containing filesystem layout details.

        Returns:
            The hex color code for the sector.
        """
        if not layout_info:
            return DEFAULT_COLOR

        type_color_map: dict[str, str] = layout_info.get("type_color_map", {})

        try:
            if hasattr(self, "_selected_units") and self._selected_units and lba >= 0:
                allocation_unit_size = layout_info.get(
                    "allocation_unit_size_sectors", 1
                )
                first_data_sector = layout_info.get("first_data_sector", 0)

                if lba >= first_data_sector and allocation_unit_size > 0:
                    allocation_unit = (lba - first_data_sector) // allocation_unit_size

                    if allocation_unit in self._selected_units:
                        return SELECTED_FILE_COLOR

            get_type_func: Optional[Callable[[int], str]] = layout_info.get(
                "get_sector_type"
            )
            if get_type_func:
                sector_type = get_type_func(lba)
                return type_color_map.get(sector_type, DEFAULT_COLOR)

            return DEFAULT_COLOR

        except Exception as e:
            app_logger = self._get_app_logger()
            log_msg = f"Error getting sector color for LBA {lba}: {e}"
            if app_logger:
                app_logger.error(log_msg)
            else:
                logger.error(log_msg)
            return ERROR_COLOR
