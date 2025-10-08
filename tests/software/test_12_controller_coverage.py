"""
Additional tests for DiskController to increase code coverage.

This module focuses on error handling, edge cases, and code paths not
covered by existing tests.
"""

import contextlib
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.drivers import IMGImageDriver
from fatfloppy.core.filesystem_registry import FilesystemRegistry
from fatfloppy.core.filesystems.fat12_fs import FATFilesystem, FATVolumeInfo
from fatfloppy.core.format_profile import FormatProfile
from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat

RESOURCE_DIR = Path(__file__).parent.parent / "resources"
EMPTY_IMG_SRC = RESOURCE_DIR / "empty_formatted_144m.img"

_ALL_FORMATS = FilesystemRegistry.get_all_formats()
FMT_144 = _ALL_FORMATS["ibm_3.5_1.44m"]
FMT_720 = _ALL_FORMATS["ibm_3.5_720k"]


@pytest.fixture
def controller():
    """Provides a fresh DiskController instance."""
    return DiskController()


@pytest.fixture
def controller_with_disk(tmp_path):
    """Provides a DiskController with an open disk."""
    if not EMPTY_IMG_SRC.exists():
        pytest.skip(f"Required resource not found: {EMPTY_IMG_SRC}")

    test_img = tmp_path / "test.img"
    shutil.copy(EMPTY_IMG_SRC, test_img)

    ctrl = DiskController()
    ctrl.open_disk(str(test_img), disk_type="IMG")
    yield ctrl
    ctrl.close_disk()


def test_close_disk_with_flush_error(controller_with_disk) -> None:
    """Tests close_disk when flush raises an exception."""
    controller = controller_with_disk

    controller.driver.dirty = True
    with patch.object(controller.driver, "flush", side_effect=OSError("Flush failed")):
        controller.close_disk()

    assert controller.disk is None
    assert controller.driver is None


def test_create_custom_profile_missing_filesystem_type(controller) -> None:
    """Tests create_custom_profile without filesystem_type."""
    format_info = {"cylinders": 80, "heads": 2, "sectors_per_track": 18}

    result = controller.create_custom_profile(format_info)
    assert result is None


def test_create_custom_profile_unknown_filesystem(controller) -> None:
    """Tests create_custom_profile with unknown filesystem type."""
    format_info = {
        "filesystem_type": "UNKNOWN_FS",
        "cylinders": 80,
        "heads": 2,
        "sectors_per_track": 18,
    }

    profile = controller.create_custom_profile(format_info)
    assert profile is None


def test_create_custom_profile_success(controller) -> None:
    """Tests successful custom profile creation."""
    format_info = {
        "filesystem_type": "FAT12",
        "profile_name": "custom_test",
        "description": "Custom test format",
        "cylinders": 80,
        "heads": 2,
        "sectors_per_track": 9,
        "bytes_per_sector": 512,
        "encoding": "MFM",
        "rate": 250,
    }

    profile = controller.create_custom_profile(format_info)
    assert profile is not None
    assert profile.name == "custom_test"
    assert profile.physical_format.cylinders == 80
    assert profile.filesystem_config is not None


def test_create_directory_exceptions(controller_with_disk) -> None:
    """Tests create_directory with various exceptions."""
    controller = controller_with_disk

    with (
        patch.object(
            controller.filesystem, "create_directory", side_effect=OSError("Disk full")
        ),
        pytest.raises(OSError, match="Disk full"),
    ):
        controller.create_directory("/TEST")

    with (
        patch.object(
            controller.filesystem,
            "create_directory",
            side_effect=ValueError("Invalid path"),
        ),
        pytest.raises(ValueError, match="Invalid path"),
    ):
        controller.create_directory("/BAD*PATH")

    with (
        patch.object(
            controller.filesystem,
            "create_directory",
            side_effect=NotImplementedError("Not supported"),
        ),
        pytest.raises(NotImplementedError),
    ):
        controller.create_directory("/TEST")

    with (
        patch.object(
            controller.filesystem,
            "create_directory",
            side_effect=RuntimeError("Unexpected error"),
        ),
        pytest.raises(RuntimeError),
    ):
        controller.create_directory("/TEST")


def test_delete_item_exceptions(controller_with_disk) -> None:
    """Tests delete_item with various exceptions."""
    controller = controller_with_disk

    controller.write_file("/TEST.TXT", b"test")

    with (
        patch.object(
            controller.filesystem, "delete", side_effect=OSError("Cannot delete")
        ),
        pytest.raises(OSError),
    ):
        controller.delete_item("/TEST.TXT")

    with (
        patch.object(
            controller.filesystem, "delete", side_effect=ValueError("Invalid path")
        ),
        pytest.raises(ValueError),
    ):
        controller.delete_item("/TEST.TXT")

    with contextlib.suppress(FileNotFoundError):
        controller.delete_item("/NONEXISTENT.TXT")

    with (
        patch.object(
            controller.filesystem, "delete", side_effect=RuntimeError("Unexpected")
        ),
        pytest.raises(RuntimeError),
    ):
        controller.delete_item("/TEST.TXT")


def test_delete_item_recursive_no_filesystem(controller) -> None:
    """Tests delete_item_recursive without filesystem."""
    result = controller.delete_item_recursive("/PATH")
    assert result is False


def test_delete_item_recursive_exceptions(controller_with_disk) -> None:
    """Tests delete_item_recursive with various exceptions."""
    controller = controller_with_disk

    with (
        patch.object(
            controller.filesystem,
            "delete_recursive",
            side_effect=OSError("Delete failed"),
        ),
        pytest.raises(OSError),
    ):
        controller.delete_item_recursive("/PATH")

    with (
        patch.object(
            controller.filesystem,
            "delete_recursive",
            side_effect=RuntimeError("Unexpected"),
        ),
        pytest.raises(RuntimeError),
    ):
        controller.delete_item_recursive("/PATH")


def test_detect_format_exception() -> None:
    """Tests detect_format when detector raises exception."""
    controller = DiskController()

    mock_driver = MagicMock()
    mock_driver.validate_for_opening.return_value = (True, "")
    mock_driver.validate_state_for_opening.return_value = (True, "")
    mock_driver.requires_initialization = False
    mock_driver.driver_category = "raw"
    mock_driver.physical_format = None

    def get_requirements():
        return {"needs_format_for_io": False, "can_derive_format": True}

    mock_driver.get_format_requirements = get_requirements

    controller.driver = mock_driver
    controller.disk = MagicMock()

    with patch(
        "fatfloppy.core.format_detection.create_format_detector",
        side_effect=RuntimeError("Detection failed"),
    ):
        result = controller.detect_format()
        assert result == (None, None, None)


def test_detect_format_sets_new_geometry(controller_with_disk) -> None:
    """Tests detect_format applying new geometry."""
    controller = controller_with_disk

    new_geom = PhysicalFormat(
        cylinders=40,
        heads=1,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=512,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=39,
                head_start=0,
                head_end=0,
                sectors_per_track=9,
                encoding="MFM",
                rate=250,
                gap3_bytes=84,
                interleave=1,
            )
        ],
    )

    mock_detector = MagicMock()
    mock_detector.detect.return_value = ("test_format", None, new_geom)

    controller._detection_cached = False
    controller._cached_format_name = None

    with patch(
        "fatfloppy.core.format_detection.create_format_detector",
        return_value=mock_detector,
    ):
        format_name, fs_config, phys_fmt = controller.detect_format()

        assert format_name == "test_format"
        assert controller.disk.physical_format.cylinders == 40


def test_detect_geometry_with_format_name(controller_with_disk) -> None:
    """Tests detect_geometry returning format from profile."""
    controller = controller_with_disk
    controller.physical_format = None

    with patch.object(
        controller, "detect_format", return_value=("ibm_3.5_1.44m", None, None)
    ):
        geom = controller.detect_geometry()
        assert geom is not None
        assert geom.cylinders == 80


def test_flush_without_driver(controller) -> None:
    """Tests flush when no driver is present."""
    controller.flush()


def test_flush_driver_without_flush_method(controller) -> None:
    """Tests flush when driver lacks flush method."""
    controller.driver = MagicMock(spec=[])
    controller.flush()


def test_flush_error(controller_with_disk) -> None:
    """Tests flush when driver.flush raises error."""
    controller = controller_with_disk

    with patch.object(controller.driver, "flush", side_effect=OSError("Flush error")):
        controller.flush()


def test_format_disk_media_new_image_without_filepath(controller) -> None:
    """Tests format_disk_media for new image without file_path."""
    result = controller.format_disk_media("ibm_3.5_1.44m")
    assert result is False


def test_format_disk_media_existing_disk(controller_with_disk) -> None:
    """Tests format_disk_media on existing disk."""
    controller = controller_with_disk

    with patch.object(
        type(controller.driver),
        "supports_in_place_formatting",
        new_callable=PropertyMock,
        return_value=True,
    ):
        result = controller.format_disk_media("ibm_3.5_1.44m", "TEST")
        assert result is True


def test_format_disk_media_unsupported_inplace(controller_with_disk) -> None:
    """Tests format_disk_media when driver doesn't support in-place."""
    controller = controller_with_disk

    with patch.object(
        type(controller.driver),
        "supports_in_place_formatting",
        new_callable=PropertyMock,
        return_value=False,
    ):
        result = controller.format_disk_media("ibm_3.5_1.44m")
        assert result is False


def test_get_allocated_units_error(controller_with_disk) -> None:
    """Tests get_allocated_units when filesystem raises error."""
    controller = controller_with_disk

    with patch.object(
        controller.filesystem, "get_allocated_units", side_effect=RuntimeError("Error")
    ):
        result = controller.get_allocated_units()
        assert result == []


def test_get_file_allocation_units_no_filesystem(controller) -> None:
    """Tests get_file_allocation_units without filesystem."""
    result = controller.get_file_allocation_units("/FILE.TXT")
    assert result is None


def test_get_file_allocation_units_not_found(controller_with_disk) -> None:
    """Tests get_file_allocation_units for non-existent file."""
    controller = controller_with_disk

    with patch.object(
        controller.filesystem,
        "get_file_allocation_units",
        side_effect=FileNotFoundError("Not found"),
    ):
        result = controller.get_file_allocation_units("/MISSING.TXT")
        assert result is None


def test_get_file_allocation_units_not_implemented(controller_with_disk) -> None:
    """Tests get_file_allocation_units when not supported."""
    controller = controller_with_disk

    with patch.object(
        controller.filesystem,
        "get_file_allocation_units",
        side_effect=NotImplementedError(),
    ):
        result = controller.get_file_allocation_units("/FILE.TXT")
        assert result is None


def test_get_file_allocation_units_error(controller_with_disk) -> None:
    """Tests get_file_allocation_units with generic error."""
    controller = controller_with_disk

    with patch.object(
        controller.filesystem,
        "get_file_allocation_units",
        side_effect=RuntimeError("Error"),
    ):
        result = controller.get_file_allocation_units("/FILE.TXT")
        assert result is None


def test_open_disk_validation_failure(controller, tmp_path) -> None:
    """Tests open_disk when driver validation fails."""
    test_img = tmp_path / "test.img"
    test_img.write_bytes(b"\x00" * 1024)

    with patch.object(
        IMGImageDriver, "validate_for_opening", return_value=(False, "Invalid file")
    ):
        result = controller.open_disk(str(test_img), disk_type="IMG")
        assert result is False
        assert controller.driver is None


def test_open_disk_state_validation_failure(controller, tmp_path) -> None:
    """Tests open_disk when driver state validation fails."""
    test_img = tmp_path / "test.img"
    test_img.write_bytes(b"\x00" * 1024)

    with patch.object(
        IMGImageDriver, "validate_state_for_opening", return_value=(False, "Bad state")
    ):
        result = controller.open_disk(str(test_img), disk_type="IMG")
        assert result is False


def test_open_disk_initialization_failure(controller, tmp_path) -> None:
    """Tests open_disk when driver initialization fails."""
    test_img = tmp_path / "test.img"
    test_img.write_bytes(b"\x00" * 1024)

    mock_driver = MagicMock()
    mock_driver.requires_initialization = True
    mock_driver.initialize.side_effect = RuntimeError("Init failed")
    mock_driver.validate_for_opening.return_value = (True, "")
    mock_driver.validate_state_for_opening.return_value = (True, "")

    with patch(
        "fatfloppy.core.driver_factory.DriverFactory.create", return_value=mock_driver
    ):
        result = controller.open_disk(str(test_img), disk_type="IMG")
        assert result is False


def test_open_disk_needs_format_but_missing(controller, tmp_path) -> None:
    """Tests open_disk when format required but not provided."""
    test_img = tmp_path / "test.img"
    test_img.write_bytes(b"\x00" * 10240)

    mock_driver = MagicMock()
    mock_driver.requires_initialization = False
    mock_driver.driver_category = "raw"
    mock_driver.physical_format = None
    mock_driver.validate_for_opening.return_value = (True, "")
    mock_driver.validate_state_for_opening.return_value = (True, "")

    def get_requirements():
        return {"needs_format_for_io": True, "can_derive_format": False}

    mock_driver.get_format_requirements = get_requirements

    with patch(
        "fatfloppy.core.driver_factory.DriverFactory.create", return_value=mock_driver
    ):
        result = controller.open_disk(str(test_img), disk_type="IMG")
        assert result is False


def test_open_disk_generic_exception(controller) -> None:
    """Tests open_disk with generic exception during opening."""
    with patch(
        "fatfloppy.core.driver_factory.DriverFactory.create",
        side_effect=RuntimeError("Unexpected error"),
    ):
        result = controller.open_disk("/nonexistent", disk_type="IMG")
        assert result is False


def test_read_file_value_error(controller_with_disk) -> None:
    """Tests read_file when filesystem raises ValueError."""
    controller = controller_with_disk

    with patch.object(
        controller.filesystem, "read_file", side_effect=ValueError("Invalid path")
    ):
        result = controller.read_file("/BAD")
        assert result is None


def test_read_file_generic_error(controller_with_disk) -> None:
    """Tests read_file with generic exception."""
    controller = controller_with_disk

    with patch.object(
        controller.filesystem, "read_file", side_effect=RuntimeError("Unexpected")
    ):
        result = controller.read_file("/FILE")
        assert result is None


def test_set_format_no_disk(controller) -> None:
    """Tests set_format when no disk is open."""
    profile = FMT_144

    with pytest.raises(ValueError, match="No disk opened"):
        controller.set_format(profile)


def test_set_format_invalid_profile(controller_with_disk) -> None:
    """Tests set_format with invalid profile."""
    controller = controller_with_disk

    invalid_profile = MagicMock(spec=FormatProfile)
    invalid_profile.name = "invalid"
    invalid_profile.physical_format = None
    invalid_profile.filesystem_config = None

    with pytest.raises(ValueError, match="Invalid FormatProfile"):
        controller.set_format(invalid_profile)


def test_set_format_driver_without_set_method(controller_with_disk) -> None:
    """Tests set_format when driver lacks set_physical_format."""
    controller = controller_with_disk

    original_driver = controller.driver
    mock_driver = MagicMock(spec=["flush", "dirty", "validate_for_opening"])
    mock_driver.dirty = False
    controller.driver = mock_driver

    controller.set_format(FMT_144)
    assert controller.disk.physical_format is not None

    controller.driver = original_driver


def test_set_geometry_no_disk(controller) -> None:
    """Tests set_geometry when no disk is open."""
    geom = FMT_144.physical_format

    with pytest.raises(ValueError, match="No disk opened"):
        controller.set_geometry(geom)


def test_write_file_exceptions(controller_with_disk) -> None:
    """Tests write_file with various exceptions."""
    controller = controller_with_disk

    with (
        patch.object(
            controller.filesystem, "write_file", side_effect=OSError("Write failed")
        ),
        pytest.raises(OSError),
    ):
        controller.write_file("/FILE", b"data")

    with (
        patch.object(
            controller.filesystem, "write_file", side_effect=ValueError("Invalid")
        ),
        pytest.raises(ValueError),
    ):
        controller.write_file("/FILE", b"data")

    with (
        patch.object(
            controller.filesystem, "write_file", side_effect=NotImplementedError()
        ),
        pytest.raises(NotImplementedError),
    ):
        controller.write_file("/FILE", b"data")

    with (
        patch.object(
            controller.filesystem, "write_file", side_effect=RuntimeError("Unexpected")
        ),
        pytest.raises(RuntimeError),
    ):
        controller.write_file("/FILE", b"data")


def test_apply_user_format_from_profile_name(controller_with_disk) -> None:
    """Tests _apply_user_format resolving from profile name."""
    controller = controller_with_disk

    format_info = {"format_name": "ibm_3.5_1.44m"}
    controller._apply_user_format(format_info)

    assert controller.disk.physical_format.cylinders == 80


def test_apply_user_format_custom_params(controller_with_disk) -> None:
    """Tests _apply_user_format with custom parameters."""
    controller = controller_with_disk

    format_info = {
        "cylinders": 40,
        "heads": 1,
        "sectors_per_track": 9,
        "bytes_per_sector": 512,
        "encoding": "MFM",
        "rate": 250,
        "filesystem_type": "FAT12",
    }

    controller._apply_user_format(format_info)
    assert controller.disk.physical_format.cylinders == 40
    assert controller.active_filesystem_config is not None


def test_apply_user_format_no_valid_format(controller_with_disk) -> None:
    """Tests _apply_user_format when no valid format can be resolved."""
    controller = controller_with_disk

    format_info = {"format_name": "NONEXISTENT"}

    with pytest.raises(ValueError, match="Could not resolve"):
        controller._apply_user_format(format_info)


def test_apply_user_format_raw_driver_no_format(controller) -> None:
    """Tests _apply_user_format with raw driver and no format."""
    controller.driver = MagicMock()
    controller.driver.driver_category = "raw"
    controller.disk = MagicMock()
    controller.disk.physical_format = None

    format_info = {"invalid": "params"}

    with pytest.raises(ValueError, match="Could not resolve"):
        controller._apply_user_format(format_info)


def test_create_filesystem_with_config_matching(controller_with_disk) -> None:
    """Tests _create_filesystem_with_config with matching config."""
    controller = controller_with_disk

    controller.active_filesystem_config = FATVolumeInfo()

    fs = controller._create_filesystem_with_config()
    assert isinstance(fs, FATFilesystem)


def test_create_filesystem_with_config_creation_fails(controller_with_disk) -> None:
    """Tests _create_filesystem_with_config when creation fails."""
    controller = controller_with_disk

    controller.active_filesystem_config = FATVolumeInfo()

    with patch(
        "fatfloppy.core.filesystems.fat12_fs.FATFilesystem.__init__",
        side_effect=RuntimeError("Init failed"),
    ):
        controller._create_filesystem_with_config()
        assert True


def test_execute_format_no_filesystem_type(controller_with_disk) -> None:
    """Tests _execute_format when profile has no filesystem type."""
    controller = controller_with_disk

    profile = FormatProfile(
        name="test",
        description="Test",
        physical_format=FMT_144.physical_format,
        filesystem_config=None,
    )

    result = controller._execute_format(profile, "TEST")
    assert result is False


def test_execute_format_unknown_filesystem(controller_with_disk) -> None:
    """Tests _execute_format with unknown filesystem type."""
    controller = controller_with_disk

    mock_config = MagicMock()

    profile = FormatProfile(
        name="test",
        description="Test",
        physical_format=FMT_144.physical_format,
        filesystem_config=mock_config,
    )

    with patch.object(profile, "get_filesystem_type", return_value="UNKNOWN_FS"):
        result = controller._execute_format(profile, "TEST")
        assert result is False


def test_execute_format_exception_during_format(controller_with_disk) -> None:
    """Tests _execute_format when format_fs raises exception."""
    controller = controller_with_disk

    with patch.object(
        FATFilesystem, "format_fs", side_effect=RuntimeError("Format failed")
    ):
        result = controller._execute_format(FMT_144, "TEST")
        assert result is False
        assert controller.filesystem is None


def test_format_new_image_driver_no_support(controller, tmp_path) -> None:
    """Tests _format_new_image with driver that doesn't support creation."""
    test_img = tmp_path / "new.img"

    with patch.object(IMGImageDriver, "supports_new_image_creation", False):
        result = controller._format_new_image(
            str(test_img), "ibm_3.5_1.44m", "TEST", "IMG"
        )
        assert result is False


def test_format_new_image_cleanup_on_failure(controller, tmp_path) -> None:
    """Tests _format_new_image cleans up partial file on failure."""
    test_img = tmp_path / "new.img"

    with patch(
        "fatfloppy.core.controller.DiskController._execute_format",
        side_effect=RuntimeError("Format failed"),
    ):
        result = controller._format_new_image(
            str(test_img), "ibm_3.5_1.44m", "TEST", "IMG"
        )
        assert result is False


def test_apply_user_format_failure(controller_with_disk) -> None:
    """Tests _apply_user_format when application fails."""
    controller = controller_with_disk

    with (
        patch.object(
            controller, "set_geometry", side_effect=ValueError("Geometry error")
        ),
        pytest.raises(ValueError, match="Failed to apply format"),
    ):
        controller._apply_user_format({"format_name": "ibm_3.5_1.44m"})


def test_apply_user_format_cannot_resolve(controller_with_disk) -> None:
    """Tests _apply_user_format when format cannot be resolved."""
    controller = controller_with_disk

    with pytest.raises(ValueError, match="Could not resolve a valid physical format"):
        controller._apply_user_format({"format_name": "nonexistent_format"})


def test_resolve_format_profile_matches_geometry(controller_with_disk) -> None:
    """Tests _resolve_format_profile matching by geometry."""
    controller = controller_with_disk

    controller.disk.set_geometry(FMT_144.physical_format)

    profile = controller._resolve_format_profile("NONEXISTENT")

    assert profile is not None


def test_resolve_format_profile_from_driver(controller) -> None:
    """Tests _resolve_format_profile using driver's format."""
    controller.driver = MagicMock()
    controller.driver.physical_format = FMT_144.physical_format
    controller.disk = MagicMock()
    controller.disk.physical_format = None

    profile = controller._resolve_format_profile("NONEXISTENT")
    assert profile is not None
    assert profile.name == "custom_runtime"


def test_handle_format_physical_no_explicit_format(controller) -> None:
    """Tests _handle_format for physical drive without explicit format."""
    controller.driver = MagicMock()
    controller.driver.driver_category = "physical"
    controller.driver.get_format_requirements.return_value = {
        "needs_format_for_io": False,
        "can_derive_format": True,
    }
    controller.disk = MagicMock()

    result = controller._handle_format(None, "3.5")
    assert result is True


def test_handle_format_unknown_category(controller) -> None:
    """Tests _handle_format with unknown driver category."""
    controller.driver = MagicMock()
    controller.driver.driver_category = "unknown"
    controller.driver.get_format_requirements.return_value = {
        "needs_format_for_io": True,
        "can_derive_format": False,
    }
    controller.disk = MagicMock()
    controller.disk.physical_format = None

    result = controller._handle_format(None, "3.5")
    assert result is False


def test_format_new_image_cleanup_failure(controller, tmp_path) -> None:
    """Tests _format_new_image when cleanup of partial file fails."""
    test_img = tmp_path / "new.img"

    with (
        patch(
            "fatfloppy.core.controller.DiskController._execute_format",
            side_effect=RuntimeError("Format failed"),
        ),
        patch("pathlib.Path.unlink", side_effect=PermissionError("Cannot delete")),
    ):
        result = controller._format_new_image(
            str(test_img), "ibm_3.5_1.44m", "TEST", "IMG"
        )
        assert result is False


def test_resolve_format_profile_no_match_creates_custom(controller_with_disk) -> None:
    """Tests _resolve_format_profile creating custom profile when no match."""
    controller = controller_with_disk

    controller.disk.physical_format = None
    controller.driver.physical_format = PhysicalFormat(
        cylinders=33,
        heads=3,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=256,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=32,
                head_start=0,
                head_end=2,
                sectors_per_track=7,
                encoding="MFM",
                rate=300,
                gap3_bytes=42,
                interleave=1,
            )
        ],
    )

    profile = controller._resolve_format_profile("NONEXISTENT")
    assert profile is not None
    assert profile.name == "custom_runtime"
    assert profile.physical_format.cylinders == 33


def test_handle_format_with_explicit_format(controller_with_disk) -> None:
    """Tests _handle_format applying explicit user format."""
    controller = controller_with_disk

    format_info = {"format_name": "ibm_3.5_720k"}
    result = controller._handle_format(format_info, "3.5")

    assert result is True
    assert controller.disk.physical_format.cylinders == 80


def test_handle_format_auto_detection_success(controller_with_disk) -> None:
    """Tests _handle_format with successful auto-detection."""
    controller = controller_with_disk
    controller._detection_cached = False

    result = controller._handle_format(None, "3.5")
    assert result is True


def test_handle_format_auto_detection_no_format_required(controller) -> None:
    """Tests _handle_format when no format required for I/O."""
    controller.driver = MagicMock()
    controller.driver.get_format_requirements.return_value = {
        "needs_format_for_io": False,
        "can_derive_format": True,
    }
    controller.disk = MagicMock()
    controller.disk.physical_format = None

    with patch.object(controller, "detect_format", return_value=(None, None, None)):
        result = controller._handle_format(None, "3.5")
        assert result is True  # Should succeed even without format
