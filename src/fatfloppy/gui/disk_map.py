# src/fatfloppy/gui/disk_map.py
import math
from typing import List, Dict, Any, Optional, Callable
from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import (QBrush, QPen, QPolygonF, QPainter, QFont, QColor)
from PyQt6.QtWidgets import (QGraphicsEllipseItem, QGraphicsLineItem,
                             QGraphicsPolygonItem, QGraphicsScene, QGraphicsView,
                             QGraphicsSimpleTextItem)

class ResizableGraphicsView(QGraphicsView):
    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.app = parent
        self.setSceneRect(0, 0, self.width()-2, self.height()-2) # -2 fixes scrollbar appearance

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setSceneRect(0, 0, self.width()-2, self.height()-2)
        if hasattr(self.app, 'draw_disk_map'):
            self.app.draw_disk_map()

class DiskMapView:
    def __init__(self, parent):
        self.parent = parent
        self.scene = QGraphicsScene()
        self.view = ResizableGraphicsView(self.scene, parent)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def generate_arc_points(self, x0, y0, radius, theta_start, theta_end, num_points):
        points = []
        num_points = max(2, num_points)
        if num_points <= 1: return [QPointF(x0 + radius*math.cos(theta_start), y0 + radius*math.sin(theta_start))]
        delta_theta = (theta_end - theta_start) / (num_points - 1)
        for i in range(num_points):
            theta = theta_start + i * delta_theta
            x = x0 + radius * math.cos(theta)
            y = y0 + radius * math.sin(theta)
            points.append(QPointF(x, y))
        return points

    def get_sector_color(self, lba: int,
                         layout_info: Optional[Dict[str, Any]],
                         busy_units: List[int]) -> str:
        """Determine the color hex string for a sector."""
        default_color_hex = "#BEBEBE"; error_color_hex = "#8B0000"; unknown_type_color_hex = "#008B8B"
        if not layout_info: return default_color_hex
        get_sector_type_func: Optional[Callable[[int], str]] = layout_info.get('get_sector_type')
        type_color_map: Dict[str, str] = layout_info.get('type_color_map', {})
        allocation_unit_size: int = layout_info.get('allocation_unit_size_sectors', 1)
        first_data_sector: int = layout_info.get('first_data_sector', 0)
        if not get_sector_type_func: return unknown_type_color_hex
        try:
            sector_type = get_sector_type_func(lba)
            if sector_type == "data":
                if allocation_unit_size <= 0:
                    logger = getattr(getattr(self.parent, 'controller', None), 'logger', None)
                    if logger: logger.warning("Allocation unit size is zero or negative.")
                    return type_color_map.get("data_free", default_color_hex)
                relative_sector = max(0, lba - first_data_sector)
                unit_number = (relative_sector // allocation_unit_size) + 2 # Assumes FAT units start at 2 FIXME ???
                if unit_number in busy_units: return type_color_map.get("data_used", "#FF00FF")
                else: return type_color_map.get("data_free", default_color_hex)
            else: return type_color_map.get(sector_type, unknown_type_color_hex)
        except Exception as e:
            logger = getattr(getattr(self.parent, 'controller', None), 'logger', None)
            if logger: logger.error(f"Error in get_sector_color for LBA {lba}: {e}", exc_info=True)
            else: print(f"Error in get_sector_color for LBA {lba}: {e}")
            return error_color_hex

    def draw_disk_map(self, controller, current_head, busy_units,
                        free_space, total_space, app_font: QFont, text_color: QColor):
        self.scene.clear()
        view_width = self.view.width()
        view_height = self.view.height()

        if not controller or not controller.disk or not controller.physical_format:
            text = QGraphicsSimpleTextItem("No disk loaded or geometry unknown")
            text.setFont(app_font); text.setBrush(QBrush(text_color))
            text_rect = text.boundingRect()
            text.setPos((view_width - text_rect.width()) / 2, (view_height - text_rect.height()) / 2)
            self.scene.addItem(text); return

        geometry = controller.physical_format
        filesystem = controller.filesystem

        if current_head >= geometry.heads:
            text = QGraphicsSimpleTextItem(f"Invalid head selected: {current_head}")
            text.setFont(app_font); text.setBrush(QBrush(text_color)); text.setPos(10, 10)
            self.scene.addItem(text); return

        layout_info = None
        if filesystem and hasattr(filesystem, 'get_disk_map_layout'):
            layout_info = filesystem.get_disk_map_layout()
        else:
            layout_info = { 'legend': [("Unknown/Data", "#BEBEBE")], 'get_sector_type': lambda lba: "unknown", 'allocation_unit_size_sectors': 1, 'first_data_sector': 0, 'type_color_map': {"unknown": "#BEBEBE"} }

        x0 = view_width / 2; y0 = view_height / 2
        r_min = min(view_width, view_height) * 0.1
        r_max = min(view_width, view_height) * 0.45
        num_cylinders = max(1, geometry.cylinders)
        num_points = 20

        legend_x = 10; legend_y = 10
        square_size = 10; vertical_spacing = 15
        legend_items = layout_info.get('legend', [])
        if not legend_items:
            legend_text = QGraphicsSimpleTextItem("Filesystem layout unknown")
            legend_text.setFont(app_font); legend_text.setBrush(QBrush(text_color)); legend_text.setPos(legend_x, legend_y); self.scene.addItem(legend_text)
        else:
            for i, (label, color_hex) in enumerate(legend_items):
                 color = QColor(color_hex)
                 rect_item = QGraphicsPolygonItem(QPolygonF([ QPointF(legend_x, legend_y + i * vertical_spacing), QPointF(legend_x + square_size, legend_y + i * vertical_spacing), QPointF(legend_x + square_size, legend_y + i * vertical_spacing + square_size), QPointF(legend_x, legend_y + i * vertical_spacing + square_size) ]))
                 rect_item.setBrush(QBrush(color)); rect_item.setPen(QPen(Qt.GlobalColor.black, 0.5)); self.scene.addItem(rect_item)
                 text_item = QGraphicsSimpleTextItem(label); text_item.setFont(app_font); text_item.setBrush(QBrush(text_color))
                 text_rect = text_item.boundingRect(); text_y_offset = (square_size - text_rect.height()) / 2
                 text_item.setPos(legend_x + square_size + 5, legend_y + i * vertical_spacing + text_y_offset); self.scene.addItem(text_item)

        stats_x = 10; stats_y = view_height - 60
        unit_name = "units";
        stats_content = ( f"Head: {current_head}\n" f"Total: {total_space} {unit_name}\n" f"Free: {free_space} {unit_name}\n" f"Used: {total_space - free_space} {unit_name}" )
        stats_text = QGraphicsSimpleTextItem(stats_content)
        stats_text.setPos(stats_x, stats_y); stats_text.setFont(app_font); stats_text.setBrush(QBrush(text_color))
        self.scene.addItem(stats_text)

        for c in range(num_cylinders):
            try: sectors_per_track = geometry.get_sectors_per_track(c, current_head)
            except ValueError: continue
            if sectors_per_track <= 0: continue

            angle_per_sector_deg = 360.0 / sectors_per_track
            r_outer = r_max - (r_max - r_min) * c / num_cylinders
            r_inner = r_max - (r_max - r_min) * (c + 1) / num_cylinders

            for i in range(sectors_per_track):
                try:
                    lba = geometry.chs_to_lba(c, current_head, i + 1)
                    color_hex = self.get_sector_color(lba, layout_info, busy_units)
                    color = QColor(color_hex)
                    theta_start = math.radians(i * angle_per_sector_deg)
                    theta_end = math.radians((i + 1) * angle_per_sector_deg)
                    inner_points = self.generate_arc_points(x0, y0, r_inner, theta_start, theta_end, num_points)
                    outer_points = self.generate_arc_points(x0, y0, r_outer, theta_end, theta_start, num_points)
                    points = inner_points + outer_points
                    polygon = QGraphicsPolygonItem(QPolygonF(points))
                    polygon.setBrush(QBrush(color))
                    polygon.setPen(QPen(Qt.GlobalColor.black, 0.5))
                    self.scene.addItem(polygon)
                except ValueError as e:
                     logger = getattr(getattr(self.parent, 'controller', None), 'logger', None)
                     if logger: logger.error(f"Error getting LBA for C:{c} H:{current_head} S:{i+1}: {e}")
                except Exception as e:
                    logger = getattr(getattr(self.parent, 'controller', None), 'logger', None)
                    if logger: logger.error(f"Error drawing sector C:{c} H:{current_head} S:{i+1}: {e}", exc_info=True)


            for sector in range(sectors_per_track):
                theta = math.radians(sector * angle_per_sector_deg)
                p1 = QPointF(x0 + r_inner * math.cos(theta), y0 + r_inner * math.sin(theta))
                p2 = QPointF(x0 + r_outer * math.cos(theta), y0 + r_outer * math.sin(theta))
                line = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
                line.setPen(QPen(Qt.GlobalColor.black, 0.5))
                self.scene.addItem(line)

        for cylinder in range(1, num_cylinders):
            r = r_max - (r_max - r_min) * cylinder / num_cylinders
            ellipse = QGraphicsEllipseItem(x0 - r, y0 - r, 2 * r, 2 * r)
            ellipse.setPen(QPen(Qt.GlobalColor.black, 0.5))
            ellipse.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            self.scene.addItem(ellipse)

        if num_cylinders > 0:
            r_inner = r_max - (r_max - r_min) * num_cylinders / num_cylinders
            index_hole_radius = 5
            theta = 0
            index_hole_x = x0 + r_inner * math.cos(math.radians(theta)) - index_hole_radius
            index_hole_y = y0 + r_inner * math.sin(math.radians(theta)) - index_hole_radius
            index_hole = QGraphicsEllipseItem(index_hole_x, index_hole_y, 2 * index_hole_radius, 2 * index_hole_radius)
            index_hole.setBrush(QBrush(QColor("#008B8B"))) # Dark Cyan
            index_hole.setPen(QPen(Qt.GlobalColor.black, 0.5))
            self.scene.addItem(index_hole)
