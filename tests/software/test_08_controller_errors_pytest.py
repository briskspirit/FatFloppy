# tests/software/test_08_controller_errors_pytest.py
"""
Tests for the DiskController focusing on error handling and edge cases.

This test suite verifies that the DiskController behaves gracefully when its
methods are called in incorrect states (e.g., before a disk is opened or
after it has been closed). It also ensures that exceptions raised by the
underlying filesystem layer (like IsADirectoryError) are caught and handled
appropriately, returning sensible default values to the caller.
"""

import sys
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest

# Add the source directory to the Python path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.format_profile import FormatProfile
from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat


@pytest.fixture(scope="function")
def error_controller(request: pytest.FixtureRequest) -> Generator[DiskController, None, None]:
    """
    Provides a DiskController instance for error handling tests.

    This fixture patches the IMGImageDriver to prevent any actual file I/O,
    allowing for isolated testing of the controller's state logic. It also
    ensures the controller's disk is closed during teardown.

    Args:
        request: The pytest request object, used for logging.

    Yields:
        An instance of DiskController.
    """
    print(f"\n--- [Fixture Setup] Creating controller for test: {request.node.name} ---")
    controller = DiskController()
    with patch("fatfloppy.core.drivers.IMGImageDriver"):
        yield controller
    print(f"--- [Fixture Teardown] Cleaning up controller for test: {request.node.name} ---")
    if controller.disk:
        controller.close_disk()


def test_01_ops_before_open(error_controller: DiskController) -> None:
    """
    Tests that all disk operations fail gracefully or return default values
    when called before a disk has been opened.
    """
    # Arrange
    controller = error_controller
    dummy_track_format = TrackFormat(
        track_start=0, track_end=0, head_start=0, head_end=0,
        sectors_per_track=1, encoding="MFM", rate=500, gap3_bytes=84, interleave=1
    )
    dummy_geom = PhysicalFormat(
        cylinders=1, heads=1, rpm=300, heads_inverted=False,
        bytes_per_sector=512, track_formats=[dummy_track_format]
    )
    mock_profile = MagicMock(spec=FormatProfile)
    mock_profile.name = "mock_fmt"
    mock_profile.description = "Mock Format"
    mock_profile.physical_format = dummy_geom

    # Assert initial state
    assert controller.disk is None

    # Act & Assert: Verify operations fail as expected
    assert controller.detect_geometry() is None
    with pytest.raises(ValueError, match="No disk opened"):
        controller.set_geometry(dummy_geom)

    assert controller.detect_format() == (None, None, None)
    with pytest.raises(ValueError, match="No disk opened"):
        controller.set_format(mock_profile)

    assert controller.filesystem is None
    assert controller.list_directory("/") == []
    assert controller.read_file("/file.txt") is None
    assert not controller.write_file("/file.txt", b"data")
    assert not controller.create_directory("/dir")
    assert not controller.delete_item("/item")
    assert controller.get_free_space() is None
    assert controller.get_allocated_units() == []


def test_02_ops_after_close(error_controller: DiskController) -> None:
    """
    Tests that all disk operations fail gracefully or return default values
    after a disk has been closed.
    """
    # Arrange
    controller = error_controller
    dummy_track_format = TrackFormat(
        track_start=0, track_end=0, head_start=0, head_end=0,
        sectors_per_track=1, encoding="MFM", rate=500, gap3_bytes=84, interleave=1
    )
    dummy_geom = PhysicalFormat(
        cylinders=1, heads=1, rpm=300, heads_inverted=False,
        bytes_per_sector=512, track_formats=[dummy_track_format]
    )
    mock_profile = MagicMock(spec=FormatProfile)
    mock_profile.name = "mock_fmt"
    mock_profile.description = "Mock Format"
    mock_profile.physical_format = dummy_geom

    # Setup: mock a disk being open
    with patch.object(controller, "detect_format", return_value=(None, None, None)):
        controller.driver = MagicMock()
        controller.disk = MagicMock()
        controller.filesystem = MagicMock()
        controller.disk.physical_format = dummy_geom
        assert controller.disk is not None

    # Act
    controller.close_disk()

    # Assert state after close
    assert controller.disk is None

    # Act & Assert: Verify operations fail as expected
    assert controller.detect_geometry() is None
    with pytest.raises(ValueError, match="No disk opened"):
        controller.set_geometry(dummy_geom)

    assert controller.detect_format() == (None, None, None)
    with pytest.raises(ValueError, match="No disk opened"):
        controller.set_format(mock_profile)

    assert controller.filesystem is None
    assert controller.list_directory("/") == []
    assert controller.read_file("/file.txt") is None
    assert not controller.write_file("/file.txt", b"data")
    assert not controller.create_directory("/dir")
    assert not controller.delete_item("/item")
    assert controller.get_free_space() is None
    assert controller.get_allocated_units() == []


def test_03_read_dir_as_file(error_controller: DiskController) -> None:
    """
    Tests that read_file handles IsADirectoryError from the filesystem
    and returns None.
    """
    # Arrange
    controller = error_controller
    mock_filesystem = MagicMock()
    mock_filesystem.read_file.side_effect = IsADirectoryError("Path is a directory")

    # Simulate an open disk state with the mock filesystem
    controller.driver = MagicMock()
    controller.disk = MagicMock()
    controller.filesystem = mock_filesystem

    # Act
    result = controller.read_file("/mydir")

    # Assert
    assert result is None
    controller.filesystem.read_file.assert_called_once_with("/mydir")


def test_04_list_file_as_dir(error_controller: DiskController) -> None:
    """
    Tests that list_directory handles NotADirectoryError from the filesystem
    and returns an empty list.
    """
    # Arrange
    controller = error_controller
    file_path = "/mydir/myfile.txt"
    mock_filesystem = MagicMock()
    mock_filesystem.list_directory.side_effect = NotADirectoryError(f"Path is not a directory: {file_path}")

    # Simulate an open disk state with the mock filesystem
    controller.driver = MagicMock()
    controller.disk = MagicMock()
    controller.filesystem = mock_filesystem

    # Act
    result = controller.list_directory(file_path)

    # Assert
    assert result == []
    controller.filesystem.list_directory.assert_called_once_with(file_path)
