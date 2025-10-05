"""
Tests for the DiskController focusing on error handling and edge cases.

This test suite verifies that the DiskController behaves gracefully when its
methods are called in incorrect states.
"""

import sys
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.format_profile import FormatProfile
from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat


@pytest.fixture(scope="function")
def error_controller(request: pytest.FixtureRequest) -> Generator[DiskController, None, None]:
    """
    Provides a DiskController instance for error handling tests.

    Args:
        request: The pytest request object.

    Yields:
        An instance of DiskController.
    """
    controller = DiskController()
    with patch("fatfloppy.core.drivers.IMGImageDriver"):
        yield controller
    if controller.disk:
        controller.close_disk()


def test_operations_before_open(error_controller: DiskController) -> None:
    """
    Tests that all disk operations fail gracefully or return default values
    when called before a disk has been opened.
    """
    controller = error_controller
    dummy_track_format = TrackFormat(
        track_start=0,
        track_end=0,
        head_start=0,
        head_end=0,
        sectors_per_track=1,
        encoding="MFM",
        rate=500,
        gap3_bytes=84,
        interleave=1,
    )
    dummy_geom = PhysicalFormat(
        cylinders=1,
        heads=1,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=512,
        track_formats=[dummy_track_format],
    )
    mock_profile = MagicMock(spec=FormatProfile)
    mock_profile.name = "mock_fmt"
    mock_profile.description = "Mock Format"
    mock_profile.physical_format = dummy_geom

    assert controller.disk is None

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


def test_operations_after_close(error_controller: DiskController) -> None:
    """
    Tests that all disk operations fail gracefully or return default values
    after a disk has been closed.
    """
    controller = error_controller
    dummy_track_format = TrackFormat(
        track_start=0,
        track_end=0,
        head_start=0,
        head_end=0,
        sectors_per_track=1,
        encoding="MFM",
        rate=500,
        gap3_bytes=84,
        interleave=1,
    )
    dummy_geom = PhysicalFormat(
        cylinders=1,
        heads=1,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=512,
        track_formats=[dummy_track_format],
    )
    mock_profile = MagicMock(spec=FormatProfile)
    mock_profile.name = "mock_fmt"
    mock_profile.description = "Mock Format"
    mock_profile.physical_format = dummy_geom

    with patch.object(controller, "detect_format", return_value=(None, None, None)):
        controller.driver = MagicMock()
        controller.disk = MagicMock()
        controller.filesystem = MagicMock()
        controller.disk.physical_format = dummy_geom
        assert controller.disk is not None

    controller.close_disk()

    assert controller.disk is None

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


def test_read_directory_as_file(error_controller: DiskController) -> None:
    """
    Tests that read_file handles IsADirectoryError from the filesystem
    and returns None.
    """
    controller = error_controller
    mock_filesystem = MagicMock()
    mock_filesystem.read_file.side_effect = IsADirectoryError("Path is a directory")

    controller.driver = MagicMock()
    controller.disk = MagicMock()
    controller.filesystem = mock_filesystem

    result = controller.read_file("/mydir")

    assert result is None
    controller.filesystem.read_file.assert_called_once_with("/mydir")


def test_list_file_as_directory(error_controller: DiskController) -> None:
    """
    Tests that list_directory handles NotADirectoryError from the filesystem
    and returns an empty list.
    """
    controller = error_controller
    file_path = "/mydir/myfile.txt"
    mock_filesystem = MagicMock()
    mock_filesystem.list_directory.side_effect = NotADirectoryError(
        f"Path is not a directory: {file_path}"
    )

    controller.driver = MagicMock()
    controller.disk = MagicMock()
    controller.filesystem = mock_filesystem

    result = controller.list_directory(file_path)

    assert result == []
    controller.filesystem.list_directory.assert_called_once_with(file_path)
