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

# In controller.py, modify the open_disk method

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

            # For physical disk, try scanning with different geometries
            if disk_type == "physical":
                # Only try head 0 for detection
                # Try different geometries for physical disks
                geometries = [
                    # 1.44MB 3.5" HD
                    (DiskGeometry(80, 2, 18, 512), 500, "MFM"),
                    # 720KB 3.5" DD
                    (DiskGeometry(80, 2, 9, 512), 250, "MFM"),
                    # 360KB 5.25" DD
                    (DiskGeometry(40, 2, 9, 512), 250, "MFM"),
                    # 1.2MB 5.25" HD
                    (DiskGeometry(80, 2, 15, 512), 500, "MFM"),
                    # Try FM formats too
                    (DiskGeometry(40, 1, 8, 512), 125, "FM")
                ]

                for geometry, rate, encoding in geometries:
                    print(f"Trying with geometry: {geometry.cylinders}x{geometry.heads}x{geometry.sectors_per_track} ({encoding} {rate}kbps)")

                    # Set the disk geometry
                    self.set_geometry(geometry)

                    # Set the physical format
                    self.driver.set_physical_format(PhysicalFormat(
                        encoding=encoding,
                        rate=rate,
                        rpm=300,
                        gap3=84,
                        sectors_per_track=geometry.sectors_per_track,
                        heads=geometry.heads,
                        sector_size=geometry.sector_size
                    ))

                    # Try to read track 0, head 0 to see if we can get sectors
                    try:
                        # Read the boot sector
                        boot_data = self.disk.read_sector(0, 0, 1)
                        # Try to detect filesystem
                        if self.detect_filesystem():
                            # Update geometry with what was actually detected by the driver
                            if hasattr(self.driver, 'physical_format') and self.driver.physical_format:
                                actual_sectors_per_track = self.driver.physical_format.sectors_per_track
                                if actual_sectors_per_track != geometry.sectors_per_track:
                                    print(f"Updating geometry from {geometry.sectors_per_track} to {actual_sectors_per_track} sectors per track")
                                    updated_geometry = DiskGeometry(
                                        cylinders=geometry.cylinders,
                                        heads=geometry.heads,
                                        sectors_per_track=actual_sectors_per_track,
                                        sector_size=geometry.sector_size
                                    )
                                    self.set_geometry(updated_geometry)

                            print(f"Filesystem detected with geometry: {self.disk.geometry.cylinders}x{self.disk.geometry.heads}x{self.disk.geometry.sectors_per_track}")
                            return True
                    except Exception as e:
                        print(f"Failed with geometry {geometry.cylinders}x{geometry.heads}x{geometry.sectors_per_track}: {e}")
                        continue

                # If we get here, we couldn't find a valid filesystem
                # Set a default geometry for displaying something
                default_geometry = DiskGeometry(80, 2, 18, 512)
                self.set_geometry(default_geometry)
                self.driver.set_physical_format(PhysicalFormat(
                    encoding="MFM",
                    rate=500,
                    rpm=300,
                    gap3=84,
                    sectors_per_track=18,
                    heads=2,
                    sector_size=512
                ))
                print("No filesystem detected, using default geometry for display")
                return True
            else:
                # For image files, try to detect format
                format_name = self.detect_format()
                if format_name:
                    profile = self.format_manager.get_format_by_name(format_name)
                    if profile:
                        self.set_format(profile)
                        self.detect_filesystem()
                        return True

                # Try default geometries for image files
                default_geometries = [
                    DiskGeometry(80, 2, 18, 512),
                    DiskGeometry(80, 2, 9, 512),
                    DiskGeometry(40, 2, 9, 512)
                ]

                for geometry in default_geometries:
                    print(f"Trying with geometry: {geometry.cylinders}x{geometry.heads}x{geometry.sectors_per_track}")
                    self.set_geometry(geometry)
                    if self.detect_filesystem():
                        print(f"Filesystem detected with geometry: {geometry.cylinders}x{geometry.heads}x{geometry.sectors_per_track}")
                        return True

                # No format detected
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
            # Validate geometry is set
            if not self.disk.geometry:
                return None

            # Try to read the boot sector
            try:
                boot_sector = self.disk.read_sector(0, 0, 1)
                if not boot_sector or all(b == 0 for b in boot_sector):
                    print("Boot sector is empty - disk may be unformatted")
                    return None
            except Exception as e:
                print(f"Error reading boot sector: {e}")
                return None

            # Try to mount filesystem
            self.filesystem = FATFilesystem(self.disk)
            if self.filesystem.is_valid():
                # Update geometry based on boot sector information
                if hasattr(self.filesystem, 'boot_sector'):
                    bs = self.filesystem.boot_sector
                    if hasattr(bs, 'sectors_per_track') and hasattr(bs, 'num_heads'):
                        sectors_per_track = bs.sectors_per_track
                        heads = bs.num_heads

                        # If these differ from our current geometry, update it
                        if (self.disk.geometry.sectors_per_track != sectors_per_track or
                            self.disk.geometry.heads != heads):
                            print(f"Updating geometry from BPB: {sectors_per_track} sectors, {heads} heads")
                            updated_geometry = DiskGeometry(
                                cylinders=self.disk.geometry.cylinders,
                                heads=heads,
                                sectors_per_track=sectors_per_track,
                                sector_size=self.disk.geometry.sector_size
                            )
                            self.set_geometry(updated_geometry)

                            # Also update the driver's physical format
                            if self.driver and hasattr(self.driver, 'physical_format') and self.driver.physical_format:
                                self.driver.physical_format.sectors_per_track = sectors_per_track
                                self.driver.physical_format.heads = heads

                                # Create a custom disk definition if we have a physical drive
                                if hasattr(self.driver, 'create_custom_diskdef'):
                                    # Clear any existing custom disk definition if sectors mismatch
                                    if (hasattr(self.driver, 'using_custom_diskdef') and
                                            self.driver.using_custom_diskdef and
                                            self.driver.physical_format.sectors_per_track != sectors_per_track):
                                        print(f"Recreating custom disk definition with corrected sector count")
                                        self.driver.physical_format.sectors_per_track = sectors_per_track
                                        self.driver.physical_format.heads = heads
                                        self.driver._create_and_set_custom_diskdef()
                                    elif not hasattr(self.driver, 'using_custom_diskdef') or not self.driver.using_custom_diskdef:
                                        self.driver._create_and_set_custom_diskdef()

                return "FAT12"
        except Exception as e:
            print(f"Error detecting filesystem: {e}")
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
