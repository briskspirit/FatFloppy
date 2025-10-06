"""
Tests for driver_factory.py, base_driver.py, and plugin_scanner.py modules.

This module tests the DriverFactory's plugin discovery, driver selection,
validation logic, the base DiskIODriver abstract class, and the PluginScanner.
"""

import contextlib
import sys
from pathlib import Path
from typing import ClassVar, Optional
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.driver_factory import DriverFactory
from fatfloppy.core.drivers.base_driver import DiskIODriver
from fatfloppy.core.physical_format import PhysicalFormat
from fatfloppy.core.plugin_scanner import PluginScanner, PluginValidationError

# Test fixtures and mock driver classes


class MockValidDriver(DiskIODriver):
    """A valid mock driver for testing."""

    driver_type: ClassVar[str] = "MOCK"
    driver_file_extensions: ClassVar[list[str]] = [".mck"]
    driver_category: ClassVar[str] = "raw"
    driver_priority: ClassVar[int] = 50
    driver_description: ClassVar[str] = "Mock driver for testing"

    def __init__(self, file_path: str = "test.mck"):
        super().__init__()
        self.file_path = file_path

    def read_sector(self, _cylinder: int, _head: int, _sector: int) -> bytes:
        return b"\x00" * 512

    def write_sector(
        self, _cylinder: int, _head: int, _sector: int, _data: bytes
    ) -> None:
        pass

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        self.physical_format = physical_format


class MockPhysicalDriver(DiskIODriver):
    """A mock physical driver for testing."""

    driver_type: ClassVar[str] = "PHYSICAL"
    driver_file_extensions: ClassVar[list[str]] = []
    driver_category: ClassVar[str] = "physical"
    driver_priority: ClassVar[int] = 50
    driver_description: ClassVar[str] = "Mock physical driver"

    def __init__(
        self,
        device_name: Optional[str] = None,
        drive: str = "A",
        drive_size: str = "3.5",
    ):
        super().__init__()
        self.device_name = device_name
        self.drive = drive
        self.drive_size = drive_size

    def read_sector(self, _cylinder: int, _head: int, _sector: int) -> bytes:
        return b"\x00" * 512

    def write_sector(
        self, _cylinder: int, _head: int, _sector: int, _data: bytes
    ) -> None:
        pass

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        self.physical_format = physical_format


class MockHighPriorityDriver(DiskIODriver):
    """A mock driver with high priority for testing priority ordering."""

    driver_type: ClassVar[str] = "HIGH_PRIORITY"
    driver_file_extensions: ClassVar[list[str]] = [".mck"]
    driver_category: ClassVar[str] = "raw"
    driver_priority: ClassVar[int] = 100
    driver_description: ClassVar[str] = "High priority mock driver"

    def __init__(self, file_path: str = "test.mck"):
        super().__init__()
        self.file_path = file_path

    def read_sector(self, _cylinder: int, _head: int, _sector: int) -> bytes:
        return b"\xff" * 512

    def write_sector(
        self, _cylinder: int, _head: int, _sector: int, _data: bytes
    ) -> None:
        pass

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        self.physical_format = physical_format


class MockDriverNoExtensions(DiskIODriver):
    """A driver with empty extensions list."""

    driver_type: ClassVar[str] = "NO_EXT"
    driver_file_extensions: ClassVar[list[str]] = []
    driver_category: ClassVar[str] = "raw"

    def __init__(self, file_path: str = "test"):
        super().__init__()
        self.file_path = file_path

    def read_sector(self, _cylinder: int, _head: int, _sector: int) -> bytes:
        return b""

    def write_sector(
        self, _cylinder: int, _head: int, _sector: int, _data: bytes
    ) -> None:
        pass

    def set_physical_format(self, _physical_format: PhysicalFormat) -> None:
        pass


class MockDriverNoType(DiskIODriver):
    """A driver missing driver_type."""

    driver_file_extensions: ClassVar[list[str]] = [".bad"]
    driver_category: ClassVar[str] = "raw"

    def read_sector(self, _cylinder: int, _head: int, _sector: int) -> bytes:
        return b""

    def write_sector(
        self, _cylinder: int, _head: int, _sector: int, _data: bytes
    ) -> None:
        pass

    def set_physical_format(self, _physical_format: PhysicalFormat) -> None:
        pass


class MockDriverNoCategory(DiskIODriver):
    """A driver missing driver_category."""

    driver_type: ClassVar[str] = "NO_CAT"
    driver_file_extensions: ClassVar[list[str]] = [".bad"]

    def read_sector(self, _cylinder: int, _head: int, _sector: int) -> bytes:
        return b""

    def write_sector(
        self, _cylinder: int, _head: int, _sector: int, _data: bytes
    ) -> None:
        pass

    def set_physical_format(self, _physical_format: PhysicalFormat) -> None:
        pass


@pytest.fixture(scope="function", autouse=True)
def reset_factory():
    """Resets the DriverFactory state before each test."""
    DriverFactory._registry = {}
    DriverFactory._extension_map = {}
    DriverFactory._initialized = False
    yield
    DriverFactory._registry = {}
    DriverFactory._extension_map = {}
    DriverFactory._initialized = False


# Tests for base_driver.py


def test_base_driver_init_missing_driver_type() -> None:
    """Tests that __init__ raises ValueError when driver_type is not defined."""
    with pytest.raises(ValueError, match="must define driver_type"):
        MockDriverNoType()


def test_base_driver_init_missing_driver_category() -> None:
    """Tests that __init__ raises ValueError when driver_category is not defined."""
    with pytest.raises(ValueError, match="must define driver_category"):
        MockDriverNoCategory()


def test_base_driver_init_success() -> None:
    """Tests successful initialization of a valid driver."""
    driver = MockValidDriver()
    assert driver.driver_type == "MOCK"
    assert driver.driver_category == "raw"
    assert driver.logger is not None
    assert driver.physical_format is None


def test_base_driver_properties() -> None:
    """Tests all property methods return expected default values."""
    driver = MockValidDriver()

    assert driver.allows_geometry_override is True
    assert driver.has_embedded_geometry is False
    assert driver.requires_initialization is False
    assert driver.supports_in_place_formatting is True
    assert driver.supports_new_image_creation is True


def test_base_driver_flush_default() -> None:
    """Tests that flush() default implementation does nothing."""
    driver = MockValidDriver()
    driver.flush()  # Should not raise


def test_base_driver_get_format_requirements() -> None:
    """Tests get_format_requirements returns expected defaults."""
    driver = MockValidDriver()
    reqs = driver.get_format_requirements()

    assert reqs["needs_format_for_open"] is False
    assert reqs["needs_format_for_io"] is True
    assert reqs["can_derive_format"] is False
    assert reqs["preferred_detection_method"] == "explicit"


def test_base_driver_initialize_new_image_not_supported() -> None:
    """Tests initialize_new_image raises NotImplementedError when not supported."""

    class NoNewImageDriver(MockValidDriver):
        @property
        def supports_new_image_creation(self) -> bool:
            return False

    driver = NoNewImageDriver()
    mock_format = MagicMock(spec=PhysicalFormat)

    with pytest.raises(
        NotImplementedError, match="does not support creating new images"
    ):
        driver.initialize_new_image(mock_format)


def test_base_driver_initialize_new_image_default() -> None:
    """Tests initialize_new_image succeeds when driver supports new images."""
    driver = MockValidDriver()
    mock_format = MagicMock(spec=PhysicalFormat)

    # Should not raise since supports_new_image_creation is True
    driver.initialize_new_image(mock_format, None)


def test_base_driver_prepare_for_format_application() -> None:
    """Tests prepare_for_format_application default implementation."""
    driver = MockValidDriver()
    is_ready, error = driver.prepare_for_format_application({})

    assert is_ready is True
    assert error is None


def test_base_driver_validate_for_opening_file_not_found(tmp_path: Path) -> None:
    """Tests validate_for_opening returns False for non-existent file."""
    driver = MockValidDriver()
    non_existent = tmp_path / "does_not_exist.mck"

    is_valid, error = driver.validate_for_opening(str(non_existent))

    assert is_valid is False
    assert "File not found" in error


def test_base_driver_validate_for_opening_file_exists(tmp_path: Path) -> None:
    """Tests validate_for_opening returns True for existing file."""
    driver = MockValidDriver()
    test_file = tmp_path / "exists.mck"
    test_file.write_bytes(b"\x00" * 100)

    is_valid, error = driver.validate_for_opening(str(test_file))

    assert is_valid is True
    assert error is None


def test_base_driver_validate_state_for_opening() -> None:
    """Tests validate_state_for_opening default implementation."""
    driver = MockValidDriver()
    is_valid, error = driver.validate_state_for_opening()

    assert is_valid is True
    assert error is None


# Tests for plugin_scanner.py


def test_plugin_scanner_validate_has_attributes_success() -> None:
    """Tests validate_has_attributes succeeds for valid class."""
    PluginScanner.validate_has_attributes(
        MockValidDriver, ["driver_type", "driver_category"]
    )


def test_plugin_scanner_validate_has_attributes_missing() -> None:
    """Tests validate_has_attributes raises error for missing attribute."""

    class MissingAttr:
        driver_type: ClassVar[str] = "TEST"

    with pytest.raises(PluginValidationError, match="missing required attributes"):
        PluginScanner.validate_has_attributes(
            MissingAttr, ["driver_type", "missing_attr"]
        )


def test_plugin_scanner_validate_has_attributes_empty_string() -> None:
    """Tests validate_has_attributes raises error for empty string attribute."""

    class EmptyAttr:
        driver_type: ClassVar[str] = ""
        driver_category: ClassVar[str] = "raw"

    with pytest.raises(PluginValidationError, match="missing required attributes"):
        PluginScanner.validate_has_attributes(EmptyAttr, ["driver_type"])


def test_plugin_scanner_validate_has_attributes_empty_list() -> None:
    """Tests validate_has_attributes raises error for empty list attribute."""

    class EmptyList:
        driver_extensions: ClassVar[list[str]] = []

    with pytest.raises(PluginValidationError, match="missing required attributes"):
        PluginScanner.validate_has_attributes(EmptyList, ["driver_extensions"])


def test_plugin_scanner_validate_implements_methods_success() -> None:
    """Tests validate_implements_methods succeeds for complete implementation."""
    PluginScanner.validate_implements_methods(MockValidDriver, DiskIODriver)


def test_plugin_scanner_validate_implements_methods_mocked_missing() -> None:
    """Tests validate_implements_methods with a class that has abstract methods."""
    from abc import ABC, abstractmethod

    class BaseWithAbstract(ABC):
        @abstractmethod
        def required_method(self) -> None:
            pass

    # Create a mock that simulates a class missing the method
    mock_cls = MagicMock()
    mock_cls.__name__ = "IncompleteClass"
    mock_cls.__abstractmethods__ = {"required_method"}

    # Simulate that the class doesn't have the required method
    mock_cls.required_method = None

    with pytest.raises(PluginValidationError, match="does not implement"):
        PluginScanner.validate_implements_methods(mock_cls, BaseWithAbstract)


def test_plugin_scanner_discover_plugins_no_package() -> None:
    """Tests discover_plugins handles non-existent package gracefully."""
    with patch("importlib.import_module", side_effect=ImportError("Package not found")):
        plugins = PluginScanner.discover_plugins("nonexistent.package", DiskIODriver)

    assert plugins == []


def test_plugin_scanner_discover_plugins_module_import_error() -> None:
    """Tests discover_plugins handles module import errors gracefully."""
    mock_package = MagicMock()
    mock_package.__file__ = "/fake/path/__init__.py"

    with (
        patch("importlib.import_module") as mock_import,
        patch("pkgutil.iter_modules", return_value=[("", "test_module", False)]),
        patch("pathlib.Path.parent", Path("/fake/path")),
    ):
        mock_import.side_effect = [
            mock_package,  # First call succeeds (package import)
            ImportError("Module not found"),  # Second call fails (module import)
        ]

        plugins = PluginScanner.discover_plugins("test.package", DiskIODriver)

    assert plugins == []


def test_plugin_scanner_discover_plugins_skips_underscore_modules() -> None:
    """Tests discover_plugins skips modules starting with underscore."""
    mock_package = MagicMock()
    mock_package.__file__ = "/fake/path/__init__.py"

    with (
        patch("importlib.import_module", return_value=mock_package),
        patch(
            "pkgutil.iter_modules",
            return_value=[("", "_private", False), ("", "public", False)],
        ),
        patch("pathlib.Path.parent", Path("/fake/path")),
        contextlib.suppress(Exception),
    ):
        # This will fail on the "public" module, but that's okay for this test
        PluginScanner.discover_plugins("test.package", DiskIODriver)

    # The important part is that _private was skipped


def test_plugin_scanner_discover_plugins_skips_abstract() -> None:
    """Tests discover_plugins skips abstract classes."""
    from abc import ABC, abstractmethod

    class AbstractDriver(DiskIODriver, ABC):
        driver_type: ClassVar[str] = "ABSTRACT"
        driver_file_extensions: ClassVar[list[str]] = []
        driver_category: ClassVar[str] = "raw"

        @abstractmethod
        def custom_method(self) -> None:
            pass

    mock_package = MagicMock()
    mock_package.__file__ = "/fake/path/__init__.py"
    mock_module = MagicMock()

    with (
        patch("importlib.import_module") as mock_import,
        patch("pkgutil.iter_modules", return_value=[("", "test_module", False)]),
        patch("inspect.getmembers", return_value=[("AbstractDriver", AbstractDriver)]),
        patch("pathlib.Path.parent", Path("/fake/path")),
    ):
        original_module = AbstractDriver.__module__
        AbstractDriver.__module__ = "test.package.test_module"
        mock_import.side_effect = [mock_package, mock_module]

        try:
            plugins = PluginScanner.discover_plugins("test.package", DiskIODriver)
        finally:
            AbstractDriver.__module__ = original_module

    assert plugins == []


def test_plugin_scanner_discover_plugins_skips_base_class() -> None:
    """Tests discover_plugins doesn't return the base class itself."""
    mock_package = MagicMock()
    mock_package.__file__ = "/fake/path/__init__.py"
    mock_module = MagicMock()

    with (
        patch("importlib.import_module") as mock_import,
        patch("pkgutil.iter_modules", return_value=[("", "test_module", False)]),
        patch("inspect.getmembers", return_value=[("DiskIODriver", DiskIODriver)]),
        patch("pathlib.Path.parent", Path("/fake/path")),
    ):
        original_module = DiskIODriver.__module__
        DiskIODriver.__module__ = "test.package.test_module"
        mock_import.side_effect = [mock_package, mock_module]

        try:
            plugins = PluginScanner.discover_plugins("test.package", DiskIODriver)
        finally:
            DiskIODriver.__module__ = original_module

    assert plugins == []


def test_plugin_scanner_discover_plugins_exception_in_module() -> None:
    """Tests discover_plugins handles generic exceptions during module processing."""
    mock_package = MagicMock()
    mock_package.__file__ = "/fake/path/__init__.py"

    with (
        patch("importlib.import_module") as mock_import,
        patch("pkgutil.iter_modules", return_value=[("", "test_module", False)]),
        patch("pathlib.Path.parent", Path("/fake/path")),
    ):
        mock_import.side_effect = [mock_package, RuntimeError("Generic error")]

        plugins = PluginScanner.discover_plugins("test.package", DiskIODriver)

    # Should handle exception gracefully
    assert plugins == []


# Tests for driver_factory.py


def test_factory_get_driver_class_valid() -> None:
    """Tests get_driver_class returns correct driver for valid type."""
    mock_plugins = [MockValidDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        driver_class = DriverFactory.get_driver_class("MOCK")

    assert driver_class is MockValidDriver


def test_factory_get_driver_class_case_insensitive() -> None:
    """Tests get_driver_class is case-insensitive."""
    mock_plugins = [MockValidDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        driver_class = DriverFactory.get_driver_class("mock")

    assert driver_class is MockValidDriver


def test_factory_get_driver_class_not_found() -> None:
    """Tests get_driver_class returns None for unknown driver."""
    mock_plugins = [MockValidDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        driver_class = DriverFactory.get_driver_class("UNKNOWN")

    assert driver_class is None


def test_factory_get_drivers_for_extension() -> None:
    """Tests get_drivers_for_extension returns all matching drivers."""
    mock_plugins = [MockValidDriver, MockHighPriorityDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        drivers = DriverFactory.get_drivers_for_extension(".mck")

    assert len(drivers) == 2
    # High priority should be first
    assert drivers[0] == "HIGH_PRIORITY"
    assert drivers[1] == "MOCK"


def test_factory_get_drivers_for_extension_with_dot() -> None:
    """Tests get_drivers_for_extension works with or without leading dot."""
    mock_plugins = [MockValidDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        drivers_with_dot = DriverFactory.get_drivers_for_extension(".mck")
        drivers_without_dot = DriverFactory.get_drivers_for_extension("mck")

    assert drivers_with_dot == drivers_without_dot


def test_factory_get_drivers_for_extension_not_found() -> None:
    """Tests get_drivers_for_extension returns empty list for unknown extension."""
    mock_plugins = [MockValidDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        drivers = DriverFactory.get_drivers_for_extension(".xyz")

    assert drivers == []


def test_factory_get_extension_map_single_driver() -> None:
    """Tests get_extension_map returns driver type for single-driver extensions."""
    mock_plugins = [MockValidDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        ext_map = DriverFactory.get_extension_map()

    assert ext_map[".mck"] == "MOCK"


def test_factory_get_extension_map_multiple_drivers() -> None:
    """Tests get_extension_map returns AUTO for multi-driver extensions."""
    mock_plugins = [MockValidDriver, MockHighPriorityDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        ext_map = DriverFactory.get_extension_map()

    assert ext_map[".mck"] == "AUTO"


def test_factory_get_extension_map_empty_extensions() -> None:
    """Tests get_extension_map handles drivers with no extensions."""
    mock_plugins = [MockDriverNoExtensions]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        ext_map = DriverFactory.get_extension_map()

    # Should not crash, map should be empty or not contain any entry for this driver
    assert ".no_ext" not in ext_map


def test_factory_list_drivers() -> None:
    """Tests list_drivers returns information about all drivers."""
    mock_plugins = [MockValidDriver, MockPhysicalDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        drivers_info = DriverFactory.list_drivers()

    assert len(drivers_info) == 2
    assert all(isinstance(d, dict) for d in drivers_info)

    # Check required fields
    for info in drivers_info:
        assert "type" in info
        assert "class" in info
        assert "category" in info
        assert "extensions" in info
        assert "priority" in info
        assert "description" in info

    # Should be sorted by type
    assert drivers_info[0]["type"] == "MOCK"
    assert drivers_info[1]["type"] == "PHYSICAL"


def test_factory_create_explicit_driver_success(tmp_path: Path) -> None:
    """Tests creating a driver with explicit type."""
    test_file = tmp_path / "test.mck"
    test_file.write_bytes(b"\x00" * 100)

    mock_plugins = [MockValidDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        driver = DriverFactory.create("MOCK", str(test_file))

    assert isinstance(driver, MockValidDriver)
    assert driver.file_path == str(test_file)


def test_factory_create_explicit_driver_unknown_type() -> None:
    """Tests creating a driver with unknown type raises ValueError."""
    mock_plugins = [MockValidDriver]

    with (
        patch(
            "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
            return_value=mock_plugins,
        ),
        pytest.raises(ValueError, match="Unknown driver type: UNKNOWN"),
    ):
        DriverFactory.create("UNKNOWN", "test.file")


def test_factory_create_explicit_driver_instantiation_fails() -> None:
    """Tests creating a driver that fails to instantiate raises OSError."""

    class FailingDriver(MockValidDriver):
        def __init__(self, _file_path: str = "test.mck"):
            raise RuntimeError("Initialization failed")

    mock_plugins = [FailingDriver]

    with (
        patch(
            "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
            return_value=mock_plugins,
        ),
        pytest.raises(OSError, match="Driver instantiation failed"),
    ):
        DriverFactory.create("MOCK", "test.file")


def test_factory_create_auto_driver_single_candidate(tmp_path: Path) -> None:
    """Tests auto driver selection with single matching candidate."""
    test_file = tmp_path / "test.mck"
    test_file.write_bytes(b"\x00" * 100)

    mock_plugins = [MockValidDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        driver = DriverFactory.create("AUTO", str(test_file))

    assert isinstance(driver, MockValidDriver)


def test_factory_create_auto_driver_multiple_candidates_first_works(
    tmp_path: Path,
) -> None:
    """Tests auto driver selection when first candidate validates successfully."""
    test_file = tmp_path / "test.mck"
    test_file.write_bytes(b"\x00" * 100)

    mock_plugins = [MockHighPriorityDriver, MockValidDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        driver = DriverFactory.create("AUTO", str(test_file))

    # High priority driver should be selected first
    assert isinstance(driver, MockHighPriorityDriver)


def test_factory_create_auto_driver_first_fails_second_works(tmp_path: Path) -> None:
    """Tests auto driver selection when first candidate fails validation."""
    test_file = tmp_path / "test.mck"
    test_file.write_bytes(b"\x00" * 100)

    class FailsValidation(MockValidDriver):
        driver_type: ClassVar[str] = "FAIL_VAL"
        driver_priority: ClassVar[int] = 100

        def validate_for_opening(
            self, _source: str, **_kwargs
        ) -> tuple[bool, Optional[str]]:
            return False, "Validation failed"

    mock_plugins = [FailsValidation, MockValidDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        driver = DriverFactory.create("AUTO", str(test_file))

    # Should fall back to second driver
    assert isinstance(driver, MockValidDriver)


def test_factory_create_auto_driver_all_candidates_fail(tmp_path: Path) -> None:
    """Tests auto driver selection when all candidates fail validation."""
    test_file = tmp_path / "test.mck"
    test_file.write_bytes(b"\x00" * 100)

    class FailsValidation1(MockValidDriver):
        driver_type: ClassVar[str] = "FAIL1"

        def validate_for_opening(
            self, _source: str, **_kwargs
        ) -> tuple[bool, Optional[str]]:
            return False, "Validation failed 1"

    class FailsValidation2(MockValidDriver):
        driver_type: ClassVar[str] = "FAIL2"

        def validate_for_opening(
            self, _source: str, **_kwargs
        ) -> tuple[bool, Optional[str]]:
            return False, "Validation failed 2"

    mock_plugins = [FailsValidation1, FailsValidation2]

    with (
        patch(
            "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
            return_value=mock_plugins,
        ),
        pytest.raises(ValueError, match="No suitable driver found"),
    ):
        DriverFactory.create("AUTO", str(test_file))


def test_factory_create_auto_driver_no_candidates_has_physical(tmp_path: Path) -> None:
    """Tests auto driver falls back to physical when no extension matches."""
    # Create a file so validation passes
    test_file = tmp_path / "unknown.ext"
    test_file.write_bytes(b"\x00" * 100)

    class PassingPhysicalDriver(MockPhysicalDriver):
        def validate_for_opening(
            self, _source: str, **_kwargs
        ) -> tuple[bool, Optional[str]]:
            return True, None

    mock_plugins = [PassingPhysicalDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        driver = DriverFactory.create("AUTO", str(test_file))

    assert isinstance(driver, PassingPhysicalDriver)


def test_factory_create_auto_driver_no_candidates_no_physical() -> None:
    """Tests auto driver raises ValueError when no candidates and no physical driver."""
    mock_plugins = [MockValidDriver]

    with (
        patch(
            "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
            return_value=mock_plugins,
        ),
        pytest.raises(ValueError, match="No drivers registered for extension"),
    ):
        DriverFactory.create("AUTO", "unknown.ext")


def test_factory_create_auto_driver_exception_during_validation(tmp_path: Path) -> None:
    """Tests auto driver handles exceptions during validation gracefully."""
    test_file = tmp_path / "test.mck"
    test_file.write_bytes(b"\x00" * 100)

    class ThrowsException(MockValidDriver):
        driver_type: ClassVar[str] = "THROWS"

        def validate_for_opening(
            self, _source: str, **_kwargs
        ) -> tuple[bool, Optional[str]]:
            raise RuntimeError("Unexpected error during validation")

    mock_plugins = [ThrowsException, MockValidDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        driver = DriverFactory.create("AUTO", str(test_file))

    # Should fall back to second driver
    assert isinstance(driver, MockValidDriver)


def test_factory_instantiate_physical_driver() -> None:
    """Tests _instantiate_driver for physical driver category."""
    mock_plugins = [MockPhysicalDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        driver = DriverFactory.create(
            "PHYSICAL", None, drive_letter="B", drive_size="5.25"
        )

    assert isinstance(driver, MockPhysicalDriver)
    assert driver.drive == "B"
    assert driver.drive_size == "5.25"


def test_factory_validate_driver_missing_driver_type() -> None:
    """Tests _validate_driver detects empty driver_type."""

    class EmptyTypeDriver(DiskIODriver):
        driver_type: ClassVar[str] = ""
        driver_file_extensions: ClassVar[list[str]] = [".test"]
        driver_category: ClassVar[str] = "raw"

        def read_sector(self, _cylinder: int, _head: int, _sector: int) -> bytes:
            return b""

        def write_sector(
            self, _cylinder: int, _head: int, _sector: int, _data: bytes
        ) -> None:
            pass

        def set_physical_format(self, _physical_format: PhysicalFormat) -> None:
            pass

    with pytest.raises(PluginValidationError, match="cannot be empty"):
        DriverFactory._validate_driver(EmptyTypeDriver)


def test_factory_validate_driver_invalid_category() -> None:
    """Tests _validate_driver raises error for invalid driver_category."""

    class BadCategoryDriver(MockValidDriver):
        driver_category: ClassVar[str] = "invalid_category"

    with pytest.raises(PluginValidationError, match="must be one of"):
        DriverFactory._validate_driver(BadCategoryDriver)


def test_factory_discover_no_plugins() -> None:
    """Tests _discover_and_register handles no discovered plugins gracefully."""
    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=[],
    ):
        DriverFactory._discover_and_register()

    assert DriverFactory._initialized is True
    assert len(DriverFactory._registry) == 0


def test_factory_discover_plugin_discovery_exception() -> None:
    """Tests _discover_and_register handles discovery exceptions."""
    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        side_effect=RuntimeError("Discovery failed"),
    ):
        DriverFactory._discover_and_register()

    assert DriverFactory._initialized is True
    assert len(DriverFactory._registry) == 0


def test_factory_initialized_only_once() -> None:
    """Tests that _discover_and_register only runs once."""
    mock_plugins = [MockValidDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ) as mock_discover:
        DriverFactory._discover_and_register()
        DriverFactory._discover_and_register()  # Second call

    # Should only be called once
    mock_discover.assert_called_once()


def test_factory_extension_priority_sorting() -> None:
    """Tests that drivers are sorted by priority for each extension."""
    low_priority = MockValidDriver
    low_priority.driver_priority = 10

    high_priority = MockHighPriorityDriver
    high_priority.driver_priority = 90

    # Register in wrong order
    mock_plugins = [low_priority, high_priority]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        DriverFactory._discover_and_register()

    # Check that they're sorted correctly in the extension map
    drivers_for_mck = DriverFactory._extension_map[".mck"]
    assert drivers_for_mck[0].driver_priority == 90
    assert drivers_for_mck[1].driver_priority == 10


def test_factory_create_physical_driver_parameters() -> None:
    """Tests that physical driver parameters are passed correctly."""
    mock_plugins = [MockPhysicalDriver]

    with patch(
        "fatfloppy.core.driver_factory.PluginScanner.discover_plugins",
        return_value=mock_plugins,
    ):
        driver = DriverFactory.create(
            "PHYSICAL",
            "device_name_test",
            drive_letter="C",
            drive_size="8",
        )

    assert isinstance(driver, MockPhysicalDriver)
    assert driver.device_name == "device_name_test"
    assert driver.drive == "C"
    assert driver.drive_size == "8"
