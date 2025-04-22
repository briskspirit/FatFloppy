# src/fatfloppy/gui/disk_map.py
import math
from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import (QBrush, QPen, QPolygonF, QPainter)
from PyQt6.QtWidgets import (QGraphicsEllipseItem, QGraphicsLineItem,
                             QGraphicsPolygonItem, QGraphicsScene, QGraphicsView)

class ResizableGraphicsView(QGraphicsView):
    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.app = parent
        self.setSceneRect(0, 0, self.width()-2, self.height()-2) # -2 fixes scrollbar appearance

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setSceneRect(0, 0, self.width()-2, self.height()-2)
        self.app.draw_disk_map()

class DiskMapView:
    def __init__(self, parent):
        self.parent = parent
        self.scene = QGraphicsScene()
        self.view = ResizableGraphicsView(self.scene, parent)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing)

    def generate_arc_points(self, x0, y0, radius, theta_start, theta_end, num_points):
        """Generate points along an arc for polygon drawing."""
        points = []
        delta_theta = (theta_end - theta_start) / (num_points - 1)
        for i in range(num_points):
            theta = theta_start + i * delta_theta
            x = x0 + radius * math.cos(theta)
            y = y0 + radius * math.sin(theta)
            points.append(QPointF(x, y))
        return points

    def get_sector_color(self, sector_num, sectors_per_cluster,
                          reserved, fat_size, root_dir_sectors,
                          first_data_sector, busy_clusters):
        """Determine the color for a sector based on its role in FAT12 filesystem."""
        # Safety checks to prevent division by zero
        sectors_per_cluster = max(1, sectors_per_cluster)
        first_data_sector = max(1, first_data_sector)

        try:
            if sector_num < reserved:
                return Qt.GlobalColor.red  # Boot sector and reserved
            elif sector_num < reserved + fat_size:
                return Qt.GlobalColor.green  # FAT1
            elif sector_num < reserved + 2 * fat_size:
                return Qt.GlobalColor.blue  # FAT2
            elif sector_num < first_data_sector:
                return Qt.GlobalColor.yellow  # Root directory
            else:
                # Data area: color based on cluster status
                try:
                    relative_sector = max(0, sector_num - first_data_sector)
                    cluster = (relative_sector // sectors_per_cluster) + 2  # Cluster numbers start at 2
                    return Qt.GlobalColor.magenta if cluster in busy_clusters else Qt.GlobalColor.gray
                except Exception as e:
                    print(f"Error determining cluster for sector {sector_num}: {e}")
                    return Qt.GlobalColor.lightGray
        except Exception as e:
            print(f"Error in get_sector_color: {e}")
            return Qt.GlobalColor.lightGray

    def draw_disk_map(self, controller, current_head, busy_clusters, free_space, total_space, app_font):
        self.scene.clear()
        if not controller:
            view_width = self.view.width()
            view_height = self.view.height()
            text = self.scene.addText("No disk image loaded")
            text.setFont(app_font)
            text_width = text.boundingRect().width()
            text_height = text.boundingRect().height()
            text.setPos((view_width - text_width) / 2, (view_height - text_height) / 2)
            return
        if controller.geometry and current_head >= controller.geometry.heads:
            self.scene.addText("No data for this head").setPos(10, 10)
            return
        view_width = self.view.width()
        view_height = self.view.height()
        x0 = view_width / 2
        y0 = view_height / 2
        r_min = min(view_width, view_height) * 0.1
        r_max = min(view_width, view_height) * 0.45
        geometry = controller.geometry
        sectors_per_track = max(1, geometry.sectors_per_track)
        num_heads = max(1, geometry.heads)
        sector_size = max(128, geometry.sector_size)
        total_sectors = max(1, geometry.total_sectors)
        num_cylinders = max(1, geometry.cylinders)
        fs_params = self._get_filesystem_params(controller, sector_size)
        angle_per_sector = 360 / sectors_per_track
        num_points = 20
        legend_x = 10
        legend_y = 10
        square_size = 10
        vertical_spacing = 10
        colors = [
            ("Boot Sector", Qt.GlobalColor.red),
            ("FAT1", Qt.GlobalColor.green),
            ("FAT2", Qt.GlobalColor.blue),
            ("Root Directory", Qt.GlobalColor.yellow),
            ("Busy Data Sector", Qt.GlobalColor.magenta),
            ("Free Data Sector", Qt.GlobalColor.gray),
        ]
        for i, (label, color) in enumerate(colors):
            rect = QGraphicsPolygonItem(QPolygonF([
                QPointF(legend_x, legend_y + i * vertical_spacing),
                QPointF(legend_x + square_size, legend_y + i * vertical_spacing),
                QPointF(legend_x + square_size, legend_y + i * vertical_spacing + square_size),
                QPointF(legend_x, legend_y + i * vertical_spacing + square_size)
            ]))
            rect.setBrush(QBrush(color))
            self.scene.addItem(rect)
            text = self.scene.addText(label)
            text.setFont(app_font)
            text_height = text.boundingRect().height()
            y_offset = (square_size - text_height) / 2
            text.setPos(legend_x + square_size + 5, legend_y + i * vertical_spacing + y_offset)
        stats_x = 10
        stats_y = view_height - 60
        stats_text = self.scene.addText(
            f"Head: {current_head}\n"
            f"Total: {total_space} clusters\n"
            f"Free: {free_space} clusters\n"
            f"Used: {total_space - free_space} clusters"
        )
        stats_text.setPos(stats_x, stats_y)
        stats_text.setFont(app_font)
        for c in range(num_cylinders):
            cyl_start_sector = c * sectors_per_track * num_heads + (current_head * sectors_per_track)
            for i in range(sectors_per_track):
                s = cyl_start_sector + i
                if s < total_sectors:
                    color = self.get_sector_color(s, fs_params['sectors_per_cluster'],
                                                fs_params['reserved_sectors'], fs_params['fat_size'],
                                                fs_params['root_dir_sectors'], fs_params['first_data_sector'],
                                                busy_clusters)
                    theta_start = math.radians(i * angle_per_sector)
                    theta_end = math.radians((i + 1) * angle_per_sector)
                    r_outer = r_max - (r_max - r_min) * c / num_cylinders
                    r_inner = r_max - (r_max - r_min) * (c + 1) / num_cylinders
                    inner_points = self.generate_arc_points(x0, y0, r_inner, theta_start, theta_end, num_points)
                    outer_points = self.generate_arc_points(x0, y0, r_outer, theta_end, theta_start, num_points)
                    points = inner_points + outer_points
                    polygon = QGraphicsPolygonItem(QPolygonF(points))
                    polygon.setBrush(QBrush(color))
                    polygon.setPen(QPen(Qt.GlobalColor.black, 0.5))
                    self.scene.addItem(polygon)
        for sector in range(sectors_per_track):
            theta = math.radians(sector * angle_per_sector)
            p1 = QPointF(x0 + r_min * math.cos(theta), y0 + r_min * math.sin(theta))
            p2 = QPointF(x0 + r_max * math.cos(theta), y0 + r_max * math.sin(theta))
            line = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
            line.setPen(QPen(Qt.GlobalColor.black, 0.5))
            self.scene.addItem(line)
        for cylinder in range(1, num_cylinders):
            r = r_max - (r_max - r_min) * cylinder / num_cylinders
            ellipse = QGraphicsEllipseItem(x0 - r, y0 - r, 2 * r, 2 * r)
            ellipse.setPen(QPen(Qt.GlobalColor.black, 0.5))
            ellipse.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            self.scene.addItem(ellipse)

    def _get_filesystem_params(self, controller, sector_size):
        reserved_sectors = 1
        num_fats = 2
        fat_size = 9
        root_entries = 224
        sectors_per_cluster = 1

        if controller.boot_sector:
            bs = controller.boot_sector
            reserved_sectors = max(1, getattr(bs, 'reserved_sectors', 1))
            num_fats = max(1, getattr(bs, 'num_fats', 2))
            fat_size = max(1, getattr(bs, 'sectors_per_fat', 9))
            root_entries = max(16, getattr(bs, 'root_entries', 224))
            sectors_per_cluster = max(1, getattr(bs, 'sectors_per_cluster', 1))

        root_dir_sectors = (root_entries * 32 + sector_size - 1) // sector_size
        first_data_sector = reserved_sectors + num_fats * fat_size + root_dir_sectors

        return {
            'reserved_sectors': reserved_sectors,
            'num_fats': num_fats,
            'fat_size': fat_size,
            'root_dir_sectors': root_dir_sectors,
            'sectors_per_cluster': sectors_per_cluster,
            'first_data_sector': first_data_sector
        }
