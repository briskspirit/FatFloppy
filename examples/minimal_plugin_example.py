"""
Minimal, self-contained FatFloppy plugin example.

Demonstrates the three plugin surfaces and how to register them at runtime from
an external package:

  * a raw disk-image driver (``DiskIODriver``),
  * a filesystem (``Filesystem``),
  * a format detector (``FormatDetector``),

and registration via ``DriverFactory.register_external`` /
``FilesystemRegistry.register_external`` / ``DetectorRegistry.register_external``.

Run directly to register the example and list the result:

    python examples/minimal_plugin_example.py
"""

from typing import Any, ClassVar, Optional

from fatfloppy.core.detector_registry import DetectorRegistry
from fatfloppy.core.driver_factory import DriverFactory
from fatfloppy.core.drivers.base_driver import DiskIODriver
from fatfloppy.core.filesystem_registry import FilesystemRegistry
from fatfloppy.core.filesystems.fs_base import Filesystem
from fatfloppy.core.format_detection import FormatDetector
from fatfloppy.core.physical_format import PhysicalFormat


class ExampleDriver(DiskIODriver):
    """A trivial raw-image driver storing sectors in a flat byte buffer."""

    driver_type: ClassVar[str] = "EXAMPLE"
    driver_category: ClassVar[str] = "raw"
    driver_file_extensions: ClassVar[list[str]] = [".exd"]
    driver_description: ClassVar[str] = "Example raw-image driver"
    driver_priority: ClassVar[int] = 10

    def __init__(self, file_path: str):
        super().__init__()
        self.file_path = file_path
        self.physical_format: Optional[PhysicalFormat] = None
        self.image_data = bytearray()

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        bps = self.physical_format.get_bytes_per_sector(cylinder, head)
        offset = self.physical_format.chs_to_byte_offset(cylinder, head, sector)
        return bytes(self.image_data[offset : offset + bps])

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        offset = self.physical_format.chs_to_byte_offset(cylinder, head, sector)
        self.image_data[offset : offset + len(data)] = data

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        self.physical_format = physical_format

    def validate_for_opening(
        self, source: str, **_kwargs
    ) -> tuple[bool, Optional[str]]:
        return (source.lower().endswith(".exd"), None)


class ExampleFilesystem(Filesystem):
    """
    A stub filesystem that recognises a 4-byte magic in the first sector.

    Every abstract method of ``Filesystem`` is implemented (most as no-ops) so
    the class is concrete and passes plugin validation - a real plugin would
    fill these in.
    """

    filesystem_type: ClassVar[str] = "EXAMPLEFS"
    validity_threshold: ClassVar[int] = 50
    _MAGIC = b"EXFS"

    def get_validity_score(self) -> int:
        try:
            return 100 if self.disk.read_sector(0, 0, 0)[:4] == self._MAGIC else 0
        except Exception:
            return 0

    def list_directory(self, path: str = "/") -> list[Any]:
        return []

    def read_file(self, path: str) -> bytes:
        raise FileNotFoundError(path)

    def write_file(self, path: str, data: bytes) -> None:
        raise NotImplementedError("ExampleFilesystem is read-only")

    def delete(self, path: str) -> None:
        raise NotImplementedError("ExampleFilesystem is read-only")

    def delete_recursive(self, path: str) -> bool:
        raise NotImplementedError("ExampleFilesystem is read-only")

    def create_directory(self, path: str) -> None:
        raise NotImplementedError("ExampleFilesystem has no directories")

    def format_fs(self, profile: Any, volume_label: Optional[str] = None) -> None:
        raise NotImplementedError("ExampleFilesystem cannot format")

    def get_free_space(self) -> tuple[int, int]:
        return 0, 0

    def get_allocated_units(self) -> list[int]:
        return []

    def get_file_allocation_units(self, file_path: str) -> list[int]:
        return []

    def get_disk_map_layout(self) -> dict[str, Any]:
        return {}

    def get_display_info(self) -> dict[str, str]:
        return {"Filesystem Type": "EXAMPLEFS"}

    def get_specific_config(self) -> Optional[Any]:
        return None

    def configs_match(self, config_a: Any, config_b: Any) -> bool:
        return config_a == config_b


class ExampleDetector(FormatDetector):
    """A detector that pairs the example driver with the example filesystem."""

    detector_for_driver = "ExampleDriver"

    def detect(self):  # pragma: no cover - illustrative only
        return None, None, None


def register() -> None:
    """Registers the example plugin components with FatFloppy."""
    DriverFactory.register_external(driver_class=ExampleDriver)
    FilesystemRegistry.register_external(
        fs_type="EXAMPLEFS", fs_class=ExampleFilesystem
    )
    DetectorRegistry.register_external(
        driver_class_name="ExampleDriver", detector_class=ExampleDetector
    )


if __name__ == "__main__":
    register()
    print("Registered driver types:", DriverFactory.list_registered_types())
    print("EXAMPLEFS present:", FilesystemRegistry.get_by_name("EXAMPLEFS") is not None)
