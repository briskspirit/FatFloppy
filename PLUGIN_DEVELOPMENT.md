# Plugin Development Guide

## Overview
The **fatfloppy** library supports plugins for **filesystems**, **drivers**, and **format detectors** with zero‑modification extensibility. Simply place your plugin file into the appropriate directory, and it will be automatically discovered, validated, and registered at runtime.

---

## Creating a Filesystem Plugin

### Minimal Example
```python
# src/fatfloppy/core/filesystems/my_filesystem.py
from typing import List
from .fs_base import Filesystem, FileInfo

class MyFilesystem(Filesystem):
    # Required metadata
    filesystem_type = "MYFS"
    filesystem_aliases = ["MY-FS", "MYFORMAT"]
    validity_threshold = 30

    def __init__(self, disk):
        super().__init__(disk)
        # Your initialization logic here

    def get_validity_score(self) -> int:
        # Return a 0-100 confidence score based on disk analysis
        try:
            # Check for specific signatures or structures on the disk
            # For example, reading a magic number from a specific sector
            magic = self.disk.read_sector(0, 0, 1)[0:4]
            if magic == b'MYFS':
                return 95  # High confidence
            return 0
        except Exception:
            return 0

    # ... implementation of all other abstract methods ...
    def list_directory(self, path: str) -> List[FileInfo]:
        # your implementation here
        pass

    def read_file(self, path: str) -> bytes:
        # your implementation here
        pass
```

### Requirements
- **Inherit from** `fatfloppy.core.filesystems.fs_base.Filesystem`.
- **Set class variables:**
  - `filesystem_type` *(str)*: A unique, primary identifier for your filesystem.
  - `filesystem_aliases` *(List[str])*: A list of alternative names.
  - `validity_threshold` *(int)*: The minimum score (0–100) required for auto‑detection to consider this filesystem a valid match. Default is **30**.
- **Implement all abstract methods** defined in the `Filesystem` base class.
- **Place the file in** `src/fatfloppy/core/filesystems/`.

### Auto‑Discovery
Your filesystem plugin is automatically:
- **Discovered** on import.
- **Validated** for required attributes and methods.
- **Registered** with its primary type and all aliases.
- **Available** to the application via `FilesystemRegistry`.
- **Scored** during the controller's format auto‑detection process.

---

## Creating a Driver Plugin

### Minimal Example
```python
# src/fatfloppy/core/drivers/my_driver.py
from typing import List
from .base_driver import DiskIODriver

class MyCustomDriver(DiskIODriver):
    # Required metadata
    driver_type = "MYFORMAT"
    driver_file_extensions = [".myd", ".myformat"]
    driver_category = "raw"  # Can be "raw", "metadata_based", or "physical"
    driver_description = "My custom disk format driver"

    def __init__(self, file_path: str):
        super().__init__()
        self.file_path = file_path
        # Your initialization logic here

    # ... implementation of all abstract methods ...
    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        pass

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        pass
```

### Requirements
- **Inherit from** `fatfloppy.core.drivers.base_driver.DiskIODriver`.
- **Set class variables:**
  - `driver_type` *(str)*: The unique identifier used in `DriverFactory.create()`.
  - `driver_category` *(str)*: Must be one of `"metadata_based"`, `"raw"`, or `"physical"`.
  - `driver_file_extensions` *(List[str])*: A list of file extensions this driver handles.
- **Implement all abstract methods** from the `DiskIODriver` base class.
- **Place the file in** `src/fatfloppy/core/drivers/`.

### Constructor Signatures
The `DriverFactory` automatically maps arguments from `controller.open_disk()` to your driver's `__init__` method. To ensure compatibility, your constructor must use specific parameter names:

- **File‑based drivers** (`raw`, `metadata_based`): your constructor **must** accept a `file_path: str` argument.
```python
def __init__(self, file_path: str):
    # ...
```

- **Physical drivers** (`physical`): your constructor **should** accept `device_name: Optional[str]`, `drive: str`, and `drive_size: str`.
```python
from typing import Optional

def __init__(self, device_name: Optional[str], drive: str, drive_size: str):
    # ...
```

---

## Creating a Format Detector Plugin

If your custom driver (`MyCustomDriver` in this example) requires special logic for format detection, you must create a corresponding `FormatDetector` plugin.

1.  **Create a new Python file** in the `src/fatfloppy/core/drivers/detectors/` directory. The filename should correspond to your driver (e.g., `my_driver.py`).

2.  **Implement the detector class** inside this new file.

### Minimal Example

```python
# src/fatfloppy/core/drivers/detectors/my_driver.py
from typing import Optional, Any, Tuple
from ...format_detection import FormatDetector
from ...physical_format import PhysicalFormat

class MyDriverFormatDetector(FormatDetector):
    # This string MUST match the class name of your driver
    detector_for_driver = "MyCustomDriver"

    def detect(self) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        # Your custom detection logic here.
        # Analyze the disk via self.disk and self.driver.
        # Return a tuple of:
        # (format_name, filesystem_config_object, physical_format_object)

        # On failure, return (None, None, None)
        pass
```

### Requirements
- **Inherit from** `fatfloppy.core.format_detection.FormatDetector`.
- **Set** the class variable `detector_for_driver` to the **exact class name string** of the `DiskIODriver` it services.
- **Implement** the `detect` abstract method.
- **Place** the file in `src/fatfloppy/core/drivers/detectors/`.

---

## External Plugins (Outside `fatfloppy`)
For plugins distributed as separate packages, use the `register_external` methods after importing your plugin classes. This is typically done in your package's `__init__.py`.

```python
# In your external_plugin/__init__.py
from fatfloppy.core.filesystem_registry import FilesystemRegistry
from fatfloppy.core.driver_factory import DriverFactory
from fatfloppy.core.detector_registry import DetectorRegistry

from .my_driver import ExternalDriver, ExternalDetector
from .my_fs import ExternalFilesystem

# 1. Register your filesystem
FilesystemRegistry.register_external(fs_class=ExternalFilesystem)

# 2. Register your driver
DriverFactory.register_external(driver_class=ExternalDriver)

# 3. Register the custom detector for your driver
DetectorRegistry.register_external(
    driver_class_name="ExternalDriver",
    detector_class=ExternalDetector
)
```

---

## Testing Your Plugin
```python
import pytest
from fatfloppy.core.controller import DiskController
from fatfloppy.core.filesystem_registry import FilesystemRegistry
from fatfloppy.core.driver_factory import DriverFactory

def test_my_plugin_discovery():
    # Your driver should be available by its 'driver_type'
    assert "MYFORMAT" in DriverFactory.list_registered_types()

    # Your filesystem should be available by its 'filesystem_type'
    assert FilesystemRegistry.get_by_name("MYFS") is not None

def test_my_plugin_usage(tmp_path):
    controller = DiskController()

    # Create a dummy file for your driver
    test_file = tmp_path / "test.myd"
    test_file.write_bytes(b"dummy data")

    # Test opening a disk with your driver
    success = controller.open_disk(str(test_file), disk_type="MYFORMAT")
    assert success
    # Further tests...
```

---

## Validation Errors
Plugins are automatically validated when the application starts. Common errors include:
- **Missing `filesystem_type`**: The class variable must be set.
- **Missing `driver_category`**: Must be one of `"metadata_based"`, `"raw"`, or `"physical"`.
- **Unimplemented abstract methods**: You must implement all required methods from the base class.
- **Invalid `validity_threshold`**: Must be an integer between 0 and 100.

---

## Best Practices
- Keep plugins **isolated** — they should **not** import from the controller or other high‑level modules.
- **Validate inputs** within your methods.
- **Log appropriately** using `self.logger`, which is inherited from the base class.
- **Handle errors gracefully**, returning `None` or empty collections instead of crashing.
- **Write unit tests** to exercise your plugin with real or mocked disk images.
- **Document** the formats your plugin supports in its docstring.

---

## Example: Complete Minimal Plugin
See `examples/minimal_plugin_example.py` for a complete, working example that demonstrates these principles.
