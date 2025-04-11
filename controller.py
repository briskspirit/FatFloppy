# controller.py

from typing import Dict, List, Optional, Tuple, Union
import datetime
import os

from disk import Disk, DiskGeometry
from drivers import DiskIODriver, GreaseweazleDriver, RawImageDriver, PhysicalFormat
from formats import FormatManager, FormatProfile
from filesystem import Filesystem, FATFilesystem

class DiskController:
    def __init__(self):
        self.disk: Optional[Disk] = None
        self.filesystem: Optional[Filesystem] = None
        self.format_manager = FormatManager()
        self.driver: Optional[DiskIODriver] = None

    def open_disk(self, source: str, disk_type: str = "image") -> bool:
        if self.disk:
            self.close_disk()

        try:
            if disk_type == "physical":
                device_name = source if source else None
                self.driver = GreaseweazleDriver(device_name=device_name)
            elif disk_type == "image":
                if not os.path.exists(source):
                    return False
                self.driver = RawImageDriver(file_path=source)
            else:
                raise ValueError(f"Unsupported disk type: {disk_type}")

            self.disk = Disk(self.driver)

            # Try to detect format
            format_name = self.detect_format()
            if format_name:
                profile = self.format_manager.get_format_by_name(format_name)
                if profile:
                    self.set_format(profile)
                    # Now with geometry set, try to detect and mount filesystem
                    self.detect_filesystem()
                    return True

            # If format detection failed, try to set a default geometry to allow filesystem mounting
            default_geometries = [
                # 1.44MB 3.5" HD
                DiskGeometry(80, 2, 18, 512),
                # 720KB 3.5" DD
                DiskGeometry(80, 2, 9, 512),
                # 360KB 5.25" DD
                DiskGeometry(40, 2, 9, 512)
            ]

            for geometry in default_geometries:
                print(f"Trying with geometry: {geometry.cylinders}x{geometry.heads}x{geometry.sectors_per_track}")
                self.set_geometry(geometry)
                if self.detect_filesystem():
                    print(f"Filesystem detected with geometry: {geometry.cylinders}x{geometry.heads}x{geometry.sectors_per_track}")
                    return True

            # No format or filesystem detected
            return True
        except Exception as e:
            print(f"Error opening disk: {e}")
            self.close_disk()
            return False

    def close_disk(self) -> None:
        if self.disk and self.driver:
            try:
                self.driver.flush()
            except:
                pass

        self.disk = None
        self.filesystem = None
        self.driver = None

    def detect_geometry(self) -> Optional[DiskGeometry]:
        if not self.disk:
            return None

        format_name = self.detect_format()
        if format_name:
            profile = self.format_manager.get_format_by_name(format_name)
            if profile:
                return profile.geometry

        return None

    def set_geometry(self, geometry: DiskGeometry) -> None:
        if not self.disk:
            raise ValueError("No disk opened")

        self.disk.set_geometry(geometry)

    def detect_format(self) -> Optional[str]:
        if not self.disk:
            return None

        return self.format_manager.detect_format(self.disk)

    def set_format(self, profile: FormatProfile) -> None:
        if not self.disk or not self.driver:
            raise ValueError("No disk opened")

        # Set disk geometry
        self.disk.set_geometry(profile.geometry)

        # Set physical format in driver
        self.driver.set_physical_format(profile.physical_format)

    def detect_filesystem(self) -> Optional[str]:
        if not self.disk:
            return None

        # Try to mount a FAT filesystem
        try:
            self.filesystem = FATFilesystem(self.disk)
            if self.filesystem.is_valid():
                return "FAT12"
        except:
            self.filesystem = None

        return None

    def mount_filesystem(self, fs_type: str) -> bool:
        if not self.disk:
            return False

        if fs_type == "FAT12":
            self.filesystem = FATFilesystem(self.disk)
            return self.filesystem.is_valid()

        return False

    def get_allocated_clusters(self) -> List[int]:
        """Returns list of allocated cluster numbers if available"""
        if not self.filesystem or not hasattr(self.filesystem, "get_allocated_clusters"):
            return []

        try:
            return self.filesystem.get_allocated_clusters()
        except:
            return []

    def format_disk(self, format_profile: FormatProfile, fs_type: str = "FAT12") -> bool:
        if not self.disk or not self.driver:
            return False

        try:
            # Set the format
            self.set_format(format_profile)

            # Format the filesystem
            if fs_type == "FAT12":
                self.filesystem = FATFilesystem(self.disk)
                self.filesystem.format_fs()
                return True
            else:
                raise ValueError(f"Unsupported filesystem type: {fs_type}")
        except Exception as e:
            print(f"Error formatting disk: {e}")
            return False

    def list_directory(self, path: str = "/") -> List[dict]:
        if not self.filesystem:
            return []

        try:
            items = self.filesystem.list_directory(path)
            return [
                {
                    "name": item.name,
                    "size": item.size,
                    "is_dir": item.is_dir,
                    "datetime": item.datetime,
                    "attributes": item.attributes
                }
                for item in items
            ]
        except Exception as e:
            print(f"Error listing directory {path}: {e}")
            return []

    def read_file(self, path: str) -> Optional[bytes]:
        if not self.filesystem:
            return None

        try:
            return self.filesystem.read_file(path)
        except Exception as e:
            print(f"Error reading file {path}: {e}")
            return None

    def write_file(self, path: str, data: bytes) -> bool:
        if not self.filesystem:
            return False

        try:
            self.filesystem.write_file(path, data)
            return True
        except Exception as e:
            print(f"Error writing file {path}: {e}")
            return False

    def create_directory(self, path: str) -> bool:
        if not self.filesystem:
            return False

        try:
            self.filesystem.create_directory(path)
            return True
        except Exception as e:
            print(f"Error creating directory {path}: {e}")
            return False

    def delete_item(self, path: str) -> bool:
        if not self.filesystem:
            return False

        try:
            self.filesystem.delete(path)
            return True
        except Exception as e:
            print(f"Error deleting item {path}: {e}")
            return False

    def list_formats(self) -> List[Tuple[str, str]]:
        return self.format_manager.list_known_formats()

    def get_free_space(self) -> Optional[Tuple[int, int]]:
        """Returns (free_bytes, total_bytes) if available"""
        if not self.filesystem or not hasattr(self.filesystem, "get_free_space"):
            return None

        try:
            return self.filesystem.get_free_space()
        except:
            return None
