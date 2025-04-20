# src/fatfloppy/core/controller.py
import os
from typing import List, Optional, Tuple

from .utils.logging_config import get_logger
from .disk import Disk, DiskGeometry
from .drivers import DiskIODriver, GreaseweazleDriver, RawImageDriver
from .physical_format import PhysicalFormat
from .formats import BootSectorData, FormatProfile
from .filesystem import Filesystem, FATFilesystem
from .format_definitions import FLOPPY_FORMATS

logger = get_logger()

class DiskController:
    def __init__(self):
        self.logger = get_logger(self.__class__.__name__)
        self.disk: Optional[Disk] = None
        self.filesystem: Optional[Filesystem] = None
        self.known_formats = FLOPPY_FORMATS
        self.driver: Optional[DiskIODriver] = None
        self.explicit_format_set = False
        self.geometry = None
        self.boot_sector = None

    def _create_physical_driver(self, source: str, drive_letter: str, drive_size: str) -> GreaseweazleDriver:
        device_name = source if source else None
        driver = GreaseweazleDriver(device_name=device_name, drive=drive_letter, drive_size=drive_size)
        try:
            driver.initialize()
        except Exception as e:
            self.logger.error(f"Error initializing Greaseweazle driver: {e}")
            raise
        return driver

    def _create_image_driver(self, source: str) -> RawImageDriver:
        if not os.path.exists(source):
            raise FileNotFoundError(f"Image file not found: {source}")
        return RawImageDriver(file_path=source)

    def _apply_user_format(self, format_info: dict) -> None:
        self.explicit_format_set = True
        format_name = format_info.get("format_name")
        base_profile = self.get_format_by_name(format_name) if format_name else None
        default_phys = PhysicalFormat(encoding="MFM", rate=500, rpm=300, gap3=84, cskew=0, interleave=1)
        if base_profile and base_profile.physical_format:
            default_phys = base_profile.physical_format
        physical_format = PhysicalFormat(
            encoding=format_info.get("encoding", default_phys.encoding),
            rate=format_info.get("rate", default_phys.rate),
            rpm=format_info.get("rpm", default_phys.rpm),
            gap3=format_info.get("gap3", default_phys.gap3),
            cskew=format_info.get("cskew", default_phys.cskew),
            interleave=format_info.get("interleave", default_phys.interleave)
        )
        self.driver.set_physical_format(physical_format)
        default_geom = DiskGeometry(cylinders=80, heads=2, sectors_per_track=18, sector_size=512)
        if base_profile:
            default_geom = base_profile.geometry
        geometry = DiskGeometry(
            cylinders=format_info.get("cylinders", default_geom.cylinders),
            heads=format_info.get("heads", default_geom.heads),
            sectors_per_track=format_info.get("sectors_per_track", default_geom.sectors_per_track),
            sector_size=format_info.get("sector_size", default_geom.sector_size)
        )
        self.disk.set_geometry(geometry)
        if isinstance(self.driver, GreaseweazleDriver) and hasattr(self.driver, '_create_and_set_custom_diskdef'):
            self.driver._create_and_set_custom_diskdef(geometry.cylinders)

    def open_disk(self, source: str, disk_type: str = "image", drive_letter: str = "A", drive_size: str = "3.5", format_info: dict = None) -> bool:
        if self.disk:
            self.close_disk()
        self.explicit_format_set = False
        try:
            if disk_type == "physical":
                self.driver = self._create_physical_driver(source, drive_letter, drive_size)
                self.disk = Disk(self.driver)
                if format_info:
                    self._apply_user_format(format_info)
                    self.detect_filesystem()
                else:
                    result = self._detect_physical_disk_format(drive_size)
                    if not result:
                        return False
            elif disk_type == "image":
                self.driver = self._create_image_driver(source)
                self.disk = Disk(self.driver)
                result = self._detect_image_file_format(source)
                if not result:
                    return False
            else:
                raise ValueError(f"Unsupported disk type: {disk_type}")
            self.geometry = self.disk.geometry if self.disk else None
            self.boot_sector = self.filesystem.boot_sector if self.filesystem else None
            return True
        except Exception as e:
            self.logger.exception(f"Error opening disk: {e}")
            self.close_disk()
            return False

    def close_disk(self) -> None:
        if self.disk and self.driver:
            try:
                self.driver.flush()
            except Exception as e:
                self.logger.error(f"Error flushing driver: {e}")
        self.disk = None
        self.filesystem = None
        self.driver = None

    def detect_geometry(self) -> Optional[DiskGeometry]:
        if not self.disk:
            self.logger.error("No disk opened to detect geometry")
            return None
        format_name = self.detect_format()
        if format_name:
            profile = self.get_format_by_name(format_name)
            if profile:
                return profile.geometry
        return None

    def set_geometry(self, geometry: DiskGeometry) -> None:
        if not self.disk:
            self.logger.error("No disk opened to set geometry")
            raise ValueError("No disk opened")
        self.disk.set_geometry(geometry)

    def detect_format(self) -> Optional[str]:
        if not self.disk or not self.driver:
            self.logger.error("No disk opened to detect format")
            return None
        try:
            if hasattr(self.driver, 'read_bytes_direct'):
                boot_sector_bytes = self.driver.read_bytes_direct(0, 512)
            else:
                if not self.disk.geometry:
                    temp_geom = DiskGeometry(80, 2, 9, 512)
                    self.disk.set_geometry(temp_geom)
                    if hasattr(self.driver, "set_physical_format") and not getattr(self.driver, "physical_format", None):
                        temp_phys = PhysicalFormat(encoding="MFM", rate=250, rpm=300, sectors_per_track=9, heads=2, sector_size=512)
                        self.driver.set_physical_format(temp_phys)
                boot_sector_bytes = self.disk.read_sector(0, 0, 1)
            if not boot_sector_bytes or len(boot_sector_bytes) < 512:
                self.logger.warning(f"Failed to read valid boot sector (read {len(boot_sector_bytes or b'')} bytes). Cannot detect format.")
                return None
            try:
                boot_data = BootSectorData.from_bytes(boot_sector_bytes)
            except ValueError as e:
                self.logger.warning(f"Could not parse boot sector data: {e}")
                return None
            for format_name, profile in self.known_formats.items():
                if (profile.boot_sector and
                    profile.boot_sector.sectors_per_track == boot_data.sectors_per_track and
                    profile.boot_sector.num_heads == boot_data.num_heads and
                    profile.boot_sector.total_sectors == boot_data.total_sectors and
                    profile.boot_sector.media_descriptor == boot_data.media_descriptor):
                    return format_name
                elif (profile.geometry.sectors_per_track == boot_data.sectors_per_track and
                      profile.geometry.heads == boot_data.num_heads and
                      profile.geometry.total_sectors == boot_data.total_sectors):
                    return format_name
            return None
        except Exception as e:
            self.logger.error(f"Error during format detection: {e}", exc_info=True)
            return None

    def set_format(self, profile: FormatProfile) -> None:
        if not self.disk or not self.driver:
            self.logger.error("No disk opened to set format")
            raise ValueError("No disk opened")
        self.disk.set_geometry(profile.geometry)
        import copy
        physical_format_copy = copy.deepcopy(profile.physical_format)
        self.driver.set_physical_format(physical_format_copy)
        if isinstance(self.driver, GreaseweazleDriver) and hasattr(self.driver, '_create_and_set_custom_diskdef'):
            self.driver._create_and_set_custom_diskdef(profile.geometry.cylinders)

    def detect_filesystem(self) -> Optional[str]:
        if not self.disk:
            self.logger.error("No disk opened to detect filesystem")
            return None
        try:
            if not self.disk.geometry:
                return None
            boot_sector = self.disk.read_sector(0, 0, 1)
            if not boot_sector or all(b == 0 for b in boot_sector):
                return None
            self.filesystem = FATFilesystem(self.disk)
            if self.filesystem.is_valid():
                if hasattr(self.driver, 'physical_format') and self.driver.physical_format:
                    actual_sectors = self.driver.physical_format.sectors_per_track
                    if actual_sectors != self.disk.geometry.sectors_per_track:
                        updated_geometry = DiskGeometry(
                            cylinders=self.disk.geometry.cylinders,
                            heads=self.disk.geometry.heads,
                            sectors_per_track=actual_sectors,
                            sector_size=self.disk.geometry.sector_size
                        )
                        self.set_geometry(updated_geometry)
                elif not self.explicit_format_set and hasattr(self.filesystem, 'boot_sector'):
                    bs = self.filesystem.boot_sector
                    if (hasattr(bs, 'sectors_per_track') and bs.sectors_per_track > 0 and
                        hasattr(bs, 'num_heads') and bs.num_heads > 0):
                        sectors_per_track = bs.sectors_per_track
                        heads = bs.num_heads
                        if (self.disk.geometry.sectors_per_track != sectors_per_track or
                            self.disk.geometry.heads != heads):
                            updated_geometry = DiskGeometry(
                                cylinders=self.disk.geometry.cylinders,
                                heads=heads,
                                sectors_per_track=sectors_per_track,
                                sector_size=self.disk.geometry.sector_size
                            )
                            self.set_geometry(updated_geometry)
                return "FAT12"
        except Exception as e:
            self.logger.exception(f"Error detecting filesystem: {e}")
            self.filesystem = None
        return None

    def get_allocated_clusters(self) -> List[int]:
        if not self.filesystem or not hasattr(self.filesystem, "get_allocated_clusters"):
            return []
        try:
            clusters = self.filesystem.get_allocated_clusters()
            return clusters
        except Exception as e:
            self.logger.error(f"Error getting allocated clusters: {e}")
            return []

    def get_free_space(self) -> Optional[Tuple[int, int]]:
        if not self.filesystem or not hasattr(self.filesystem, "get_free_space"):
            return None
        try:
            space_info = self.filesystem.get_free_space()
            return space_info
        except Exception as e:
            self.logger.error(f"Error getting free space: {e}")
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
                    "attributes": item.attributes,
                    "starting_cluster": item.starting_cluster
                }
                for item in items
            ]
        except Exception as e:
            self.logger.error(f"Error listing directory {path}: {e}")
            return []

    def read_file(self, path: str) -> Optional[bytes]:
        if not self.filesystem:
            return None
        try:
            data = self.filesystem.read_file(path)
            return data
        except ValueError as e:
            return None
        except Exception as e:
            self.logger.error(f"Error reading file {path}: {e}")
            return None

    def write_file(self, path: str, data: bytes) -> bool:
        if not self.filesystem:
            return False
        try:
            self.filesystem.write_file(path, data)
            return True
        except Exception as e:
            self.logger.error(f"Error writing file {path}: {e}")
            return False

    def create_directory(self, path: str) -> bool:
        if not self.filesystem:
            return False
        try:
            self.filesystem.create_directory(path)
            return True
        except Exception as e:
            self.logger.error(f"Error creating directory {path}: {e}")
            return False

    def delete_item(self, path: str) -> bool:
        if not self.filesystem:
            return False
        try:
            self.filesystem.delete(path)
            return True
        except Exception as e:
            self.logger.error(f"Error deleting item {path}: {e}")
            return False

    def list_formats(self) -> List[Tuple[str, str]]:
        formats = [(name, profile.description) for name, profile in self.known_formats.items()]
        return formats

    def get_format_by_name(self, name: str) -> Optional[FormatProfile]:
        return self.known_formats.get(name)

    def _detect_physical_disk_format(self, drive_size: str = "3.5") -> bool:
        default_cylinders = 80
        if drive_size == "5.25":
            default_cylinders = 40
        elif drive_size == "8":
            default_cylinders = 77
        temp_geometry = DiskGeometry(
            cylinders=default_cylinders,
            heads=2,
            sectors_per_track=18 if drive_size == "3.5" else (9 if drive_size == "5.25" else 26),
            sector_size=512 if drive_size != "8" else 128
        )
        temp_rate = 500 if drive_size == "3.5" else (250 if drive_size == "5.25" else 250)
        temp_encoding = "MFM" if drive_size != "8" else "FM"
        temp_rpm = 300 if drive_size != "8" else 360
        temp_physical = PhysicalFormat(
            encoding=temp_encoding, rate=temp_rate, rpm=temp_rpm,
            sectors_per_track=temp_geometry.sectors_per_track,
            heads=temp_geometry.heads, sector_size=temp_geometry.sector_size
        )
        temp_profile = FormatProfile(
            name="temp_detect", description="Temporary for detection",
            geometry=temp_geometry, physical_format=temp_physical
        )
        self.set_format(temp_profile)
        if hasattr(self.driver, 'initialize'):
            self.driver.initialize()
        has_second_head = True
        fs_type = None
        self.set_geometry(temp_geometry)
        try:
            fs_type = self.detect_filesystem()
            if fs_type and self.filesystem and hasattr(self.filesystem, 'boot_sector'):
                bs = self.filesystem.boot_sector
                if hasattr(bs, 'num_heads') and bs.num_heads > 0:
                    has_second_head = bs.num_heads > 1
                    return True
        except Exception as e:
            pass
        if fs_type is None:
            try:
                if hasattr(self.driver, '_read_track') and isinstance(self.driver, GreaseweazleDriver):
                    self.set_format(temp_profile)
                    success = self.driver._read_track(0, 1)
                    if success:
                        track_data = self.driver.track_data.get((0, 1), {})
                        if track_data:
                            has_second_head = True
                        else:
                            has_second_head = False
                    else:
                        has_second_head = False
                else:
                    try:
                        if temp_geometry.heads < 2:
                            temp_geometry.heads = 2
                            self.set_geometry(temp_geometry)
                        self.disk.read_sector(0, 1, 1)
                        has_second_head = True
                    except Exception:
                        has_second_head = False
            except Exception as e:
                has_second_head = True
        filtered_formats = []
        try:
            for name, profile in self.known_formats.items():
                size_match = False
                if drive_size == "3.5" and "3.5\"" in profile.description: size_match = True
                elif drive_size == "5.25" and "5.25\"" in profile.description: size_match = True
                elif drive_size == "8" and "8\"" in profile.description: size_match = True
                if not size_match:
                    continue
                if not has_second_head and profile.geometry.heads > 1:
                    continue
                filtered_formats.append(profile)
        except Exception as e:
            return False
        for profile in filtered_formats:
            try:
                self.set_format(profile)
                fs_type = self.detect_filesystem()
                if fs_type:
                    return True
            except Exception as e:
                continue
        default_heads = 1 if not has_second_head else 2
        final_geometry = DiskGeometry(
            cylinders=default_cylinders,
            heads=default_heads,
            sectors_per_track=temp_profile.geometry.sectors_per_track,
            sector_size=temp_profile.geometry.sector_size
        )
        final_physical = PhysicalFormat(
            encoding=temp_profile.physical_format.encoding,
            rate=temp_profile.physical_format.rate,
            rpm=temp_profile.physical_format.rpm,
            gap3=temp_profile.physical_format.gap3,
            cskew=temp_profile.physical_format.cskew,
            interleave=temp_profile.physical_format.interleave,
            sectors_per_track=final_geometry.sectors_per_track,
            heads=final_geometry.heads,
            sector_size=final_geometry.sector_size
        )
        final_profile = FormatProfile(
            name="fallback_detected", description="Fallback based on detection",
            geometry=final_geometry, physical_format=final_physical
        )
        self.set_format(final_profile)
        return True

    def _detect_image_file_format(self, file_path: str) -> bool:
        format_name = self.detect_format()
        if format_name:
            profile = self.get_format_by_name(format_name)
            if profile:
                self.set_format(profile)
                if self.detect_filesystem():
                    return True
                else:
                    return True
        try:
            file_size = os.path.getsize(file_path)
            matched_profiles = []
            for name, profile in self.known_formats.items():
                if profile.geometry.total_bytes == file_size:
                    matched_profiles.append(profile)
            for profile in matched_profiles:
                self.set_format(profile)
                if self.detect_filesystem():
                    return True
            if not matched_profiles:
                default_geometries = [
                    DiskGeometry(80, 2, 18, 512),
                    DiskGeometry(80, 2, 9, 512),
                    DiskGeometry(40, 2, 9, 512),
                    DiskGeometry(80, 2, 21, 512),
                    DiskGeometry(80, 2, 36, 512),
                    DiskGeometry(40, 1, 9, 512),
                ]
                for geometry in default_geometries:
                    temp_physical = PhysicalFormat(
                        encoding="MFM", rate=500, rpm=300,
                        sectors_per_track=geometry.sectors_per_track,
                        heads=geometry.heads, sector_size=geometry.sector_size
                    )
                    temp_profile = FormatProfile(
                        name="temp_default", description="Temporary default",
                        geometry=geometry, physical_format=temp_physical
                    )
                    self.set_format(temp_profile)
                    if file_size >= geometry.total_bytes:
                        if self.detect_filesystem():
                            return True
            if not self.disk.geometry:
                default_profile = self.get_format_by_name("ibm_3.5_1.44m")
                if default_profile:
                    self.set_format(default_profile)
                else:
                    fallback_geom = DiskGeometry(80, 2, 18, 512)
                    fallback_phys = PhysicalFormat(encoding="MFM", rate=500, rpm=300, sectors_per_track=18, heads=2, sector_size=512)
                    self.disk.set_geometry(fallback_geom)
                    if hasattr(self.driver, "set_physical_format"):
                        self.driver.set_physical_format(fallback_phys)
            return True
        except Exception as e:
            return False
