# tests/software/test_08_controller_errors_pytest.py
import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat
from fatfloppy.core.format_profile import FormatProfile

@pytest.fixture(scope="function")
def error_controller(request):
    print(f"\n--- [Fixture Setup] Creating controller for test: {request.node.name} ---")
    controller = DiskController()
    with patch('fatfloppy.core.drivers.IMGImageDriver') as MockRawDriver:
        yield controller
    print(f"--- [Fixture Teardown] Cleaning up controller for test: {request.node.name} ---")
    if controller.disk:
        controller.close_disk()

def test_01_ops_before_open(error_controller):
    controller = error_controller
    assert controller.disk is None # Verify starting state

    # Check operations before open
    assert controller.detect_geometry() is None

    # --- FIX: Create PhysicalFormat correctly ---
    dummy_track_format = TrackFormat(
        track_start=0, track_end=0, head_start=0, head_end=0,
        sectors_per_track=1, encoding="MFM", rate=500, gap3=84, interleave=1
    )
    dummy_geom = PhysicalFormat(
        cylinders=1, heads=1, rpm=300, heads_inverted=False,
        bytes_per_sector=512, track_formats=[dummy_track_format]
    )
    with pytest.raises(ValueError, match="No disk opened"):
        controller.set_geometry(dummy_geom)
    # --- End Fix ---

    assert controller.detect_format() == (None, None), "detect_format should return (None, None) before open"

    # --- FIX: Create PhysicalFormat for mock profile correctly ---
    mock_profile = MagicMock(spec=FormatProfile) # Use spec for better mocking
    mock_profile.name = "mock_fmt"
    mock_profile.description = "Mock Format"
    mock_profile.physical_format = dummy_geom # Reuse the correctly created dummy geom
    with pytest.raises(ValueError, match="No disk opened"):
        controller.set_format(mock_profile)
    # --- End Fix ---

    # Remaining assertions are likely correct
    assert controller.filesystem is None
    assert controller.list_directory("/") == []
    assert controller.read_file("/file.txt") is None
    assert not controller.write_file("/file.txt", b"data")
    assert not controller.create_directory("/dir")
    assert not controller.delete_item("/item")
    assert controller.get_free_space() is None
    assert controller.get_allocated_units() == []

def test_02_ops_after_close(error_controller):
    controller = error_controller

    # --- FIX: Define dummy geometry correctly for simulation ---
    dummy_track_format = TrackFormat(
        track_start=0, track_end=0, head_start=0, head_end=0,
        sectors_per_track=1, encoding="MFM", rate=500, gap3=84, interleave=1
    )
    dummy_geom = PhysicalFormat(
        cylinders=1, heads=1, rpm=300, heads_inverted=False,
        bytes_per_sector=512, track_formats=[dummy_track_format]
    )
    # --- End Fix ---

    # Simulate an open state briefly to close it
    with patch.object(controller, '_detect_image_file_format', return_value=True):
        controller.driver = MagicMock()
        controller.disk = MagicMock()
        controller.filesystem = MagicMock()
        controller.disk.physical_format = dummy_geom # Assign the correctly created geometry
        assert controller.disk is not None
        assert controller.driver is not None

    controller.close_disk() # Close the simulated disk
    assert controller.disk is None

    # Check operations after close
    assert controller.detect_geometry() is None
    with pytest.raises(ValueError, match="No disk opened"):
        controller.set_geometry(dummy_geom) # Use the correct dummy geom

    assert controller.detect_format() == (None, None), "detect_format should return (None, None) after close"

    # --- FIX: Create PhysicalFormat for mock profile correctly ---
    mock_profile = MagicMock(spec=FormatProfile)
    mock_profile.name = "mock_fmt"
    mock_profile.description = "Mock Format"
    mock_profile.physical_format = dummy_geom # Reuse dummy geom
    with pytest.raises(ValueError, match="No disk opened"):
        controller.set_format(mock_profile)
    # --- End Fix ---

    # Remaining assertions are likely correct
    assert controller.filesystem is None
    assert controller.list_directory("/") == []
    assert controller.read_file("/file.txt") is None
    assert not controller.write_file("/file.txt", b"data")
    assert not controller.create_directory("/dir")
    assert not controller.delete_item("/item")
    assert controller.get_free_space() is None
    assert controller.get_allocated_units() == []

def test_03_read_dir_as_file(error_controller):
    controller = error_controller
    with patch.object(controller, 'open_disk', return_value=True):
        controller.driver = MagicMock()
        controller.disk = MagicMock()
        controller.filesystem = MagicMock()
        # Simulate the IsADirectoryError that the filesystem layer would raise
        controller.filesystem.read_file.side_effect = IsADirectoryError("Path is a directory")
        # Need to set up the controller state as if open_disk succeeded
        controller.driver = MagicMock()
        controller.disk = MagicMock()
        controller.filesystem = controller.filesystem # Keep the mock filesystem

        # Don't call open_disk directly as it's complex to fully mock
        # Assume controller is in an "open" state with the mock filesystem

        # Now call the method under test
        result = controller.read_file("/mydir")
        assert result is None
        controller.filesystem.read_file.assert_called_once_with("/mydir")

def test_04_list_file_as_dir(error_controller):
    controller = error_controller
    file_path = "/mydir/myfile.txt"
    with patch.object(controller, 'open_disk', return_value=True):
        controller.driver = MagicMock()
        controller.disk = MagicMock()
        controller.filesystem = MagicMock()
        # Simulate the NotADirectoryError
        controller.filesystem.list_directory.side_effect = NotADirectoryError(f"Path is not a directory: {file_path}")
        # Set up controller state
        controller.driver = MagicMock()
        controller.disk = MagicMock()
        controller.filesystem = controller.filesystem # Keep the mock filesystem

        # Call the method under test
        result = controller.list_directory(file_path)
        assert result == []
        controller.filesystem.list_directory.assert_called_once_with(file_path)
