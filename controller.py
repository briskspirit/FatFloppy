# controller.py

from typing import List, Optional, Tuple
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
            # Create appropriate driver
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

            # Different detection strategies based on disk type
            if disk_type == "physical":
                return self._detect_physical_disk_format()
            else:
                return self._detect_image_file_format(source)

        except Exception as e:
            print(f"Error opening disk: {e}")
            self.close_disk()
            return False

    def _detect_physical_disk_format(self) -> bool:
        """Detect format for physical floppy disks"""
        # Geometries to try, in order of likelihood
        geometries = [
            (DiskGeometry(80, 2, 18, 512), 500, "MFM"),  # 1.44MB 3.5" HD (most common)
            (DiskGeometry(80, 2, 15, 512), 500, "MFM"),  # 1.2MB 5.25" HD
            (DiskGeometry(80, 2, 9, 512), 250, "MFM"),   # 720KB 3.5" DD
            (DiskGeometry(40, 2, 9, 512), 250, "MFM"),   # 360KB 5.25" DD
            (DiskGeometry(40, 1, 8, 512), 125, "FM"),    # Less common FM format
        ]

        # Try each geometry until we find a valid filesystem
        for geometry, rate, encoding in geometries:
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

            # Try to detect filesystem with this geometry
            try:
                fs_type = self.detect_filesystem()
                if fs_type:
                    # Success! We've found a valid filesystem
                    return True
            except Exception as e:
                print(f"Failed with geometry {geometry.cylinders}x{geometry.heads}x{geometry.sectors_per_track}: {e}")
                continue

        # If we get here, just set a default geometry for displaying something
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

    def _detect_image_file_format(self, file_path: str) -> bool:
        """Detect format for disk image files"""
        # First try to detect format
        format_name = self.detect_format()
        if format_name:
            profile = self.format_manager.get_format_by_name(format_name)
            if profile:
                self.set_format(profile)
                if self.detect_filesystem():
                    return True

        # Try default geometries for image files
        default_geometries = [
            DiskGeometry(80, 2, 18, 512),  # 1.44MB
            DiskGeometry(80, 2, 9, 512),   # 720KB
            DiskGeometry(40, 2, 9, 512)    # 360KB
        ]

        for geometry in default_geometries:
            self.set_geometry(geometry)
            if self.detect_filesystem():
                return True

        # No format detected, but we can still work with the image
        # Just set a default geometry
        if not self.disk.geometry:
            self.set_geometry(default_geometries[0])

        return True

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
                # Always check if the driver has a physically detected sector count
                if hasattr(self.driver, 'physical_format') and self.driver.physical_format:
                    actual_sectors = self.driver.physical_format.sectors_per_track
                    # If the driver detected a different sector count than geometry, use the detected count
                    if actual_sectors != self.disk.geometry.sectors_per_track:
                        print(f"Updating geometry with physically detected sector count: {actual_sectors}")
                        updated_geometry = DiskGeometry(
                            cylinders=self.disk.geometry.cylinders,
                            heads=self.disk.geometry.heads,
                            sectors_per_track=actual_sectors,
                            sector_size=self.disk.geometry.sector_size
                        )
                        self.set_geometry(updated_geometry)

                # Now check BPB data only if we don't have custom detection
                elif hasattr(self.filesystem, 'boot_sector'):
                    bs = self.filesystem.boot_sector

                    # Only update geometry from BPB if the detected values are valid and reasonable
                    if (hasattr(bs, 'sectors_per_track') and bs.sectors_per_track > 0 and
                        hasattr(bs, 'num_heads') and bs.num_heads > 0):

                        # Update geometry from the BPB
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

                return "FAT12"
        except Exception as e:
            print(f"Error detecting filesystem: {e}")
            self.filesystem = None

        return None

    def get_allocated_clusters(self) -> List[int]:
        """Returns list of allocated cluster numbers if available"""
        if not self.filesystem or not hasattr(self.filesystem, "get_allocated_clusters"):
            return []

        try:
            return self.filesystem.get_allocated_clusters()
        except:
            return []

    def get_free_space(self) -> Optional[Tuple[int, int]]:
        """Returns (free_bytes, total_bytes) if available"""
        if not self.filesystem or not hasattr(self.filesystem, "get_free_space"):
            return None

        try:
            return self.filesystem.get_free_space()
        except:
            return None

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
