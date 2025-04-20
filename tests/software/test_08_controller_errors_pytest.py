# tests/software/test_08_controller_errors_pytest.py
import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure src is in path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.disk import DiskGeometry
from pathlib import Path # Use pathlib

# --- Fixture ---
@pytest.fixture(scope="function")
def error_controller(request):
    """Provides a clean DiskController instance."""
    print(f"\n--- [Fixture Setup] Creating controller for test: {request.node.name} ---")
    controller = DiskController()
    # Mock driver import/use for tests that might implicitly try it
    with patch('fatfloppy.core.drivers.RawImageDriver') as MockRawDriver:
        yield controller
    print(f"--- [Fixture Teardown] Cleaning up controller for test: {request.node.name} ---")
    if controller.disk:
        controller.close_disk()

# --- Tests ---

def test_01_ops_before_open(error_controller):
    controller = error_controller
    assert controller.disk is None
    assert controller.detect_geometry() is None
    with pytest.raises(ValueError, match="No disk opened"):
        dummy_geom = DiskGeometry(1,1,1,1)
        controller.set_geometry(dummy_geom)
    assert controller.detect_format() is None
    with pytest.raises(ValueError, match="No disk opened"):
        mock_profile = MagicMock(name='mock_profile')
        mock_profile.name = "mock_fmt"; mock_profile.description = "Mock Format"
        mock_profile.geometry = DiskGeometry(1,1,1,1); mock_profile.physical_format = MagicMock()
        controller.set_format(mock_profile)

    assert controller.detect_filesystem() is None
    assert controller.list_directory("/") == []
    assert controller.read_file("/file.txt") is None
    assert controller.write_file("/file.txt", b"data") is False
    assert controller.create_directory("/dir") is False
    assert controller.delete_item("/item") is False
    assert controller.get_free_space() is None
    assert controller.get_allocated_clusters() == []

def test_02_ops_after_close(error_controller):
    controller = error_controller
    # Simulate a successful open minimally for the test
    with patch.object(controller, '_detect_image_file_format', return_value=True):
        # Need to assign mocks directly as patching the class won't affect the instance here
        controller.driver = MagicMock() # Mock the driver instance
        controller.disk = MagicMock()   # Mock the disk instance
        controller.filesystem = MagicMock() # Mock the filesystem instance
        controller.disk.geometry = DiskGeometry(1,1,1,1) # Assign dummy geometry

        assert controller.disk is not None
        assert controller.driver is not None

    controller.close_disk()
    assert controller.disk is None

    # Test operations again
    assert controller.detect_geometry() is None
    with pytest.raises(ValueError, match="No disk opened"): controller.set_geometry(DiskGeometry(1,1,1,1))
    assert controller.detect_format() is None
    with pytest.raises(ValueError, match="No disk opened"): # Test set_format failure
        mock_profile = MagicMock(name='mock_profile')
        mock_profile.name = "mock_fmt"; mock_profile.description = "Mock Format"
        mock_profile.geometry = DiskGeometry(1,1,1,1); mock_profile.physical_format = MagicMock()
        controller.set_format(mock_profile)
    assert controller.detect_filesystem() is None
    assert controller.list_directory("/") == []
    assert controller.read_file("/file.txt") is None
    assert controller.write_file("/file.txt", b"data") is False
    assert controller.create_directory("/dir") is False
    assert controller.delete_item("/item") is False
    assert controller.get_free_space() is None
    assert controller.get_allocated_clusters() == []

def test_03_read_dir_as_file(error_controller):
    controller = error_controller
    # Mock open_disk and filesystem interactions
    with patch.object(controller, 'open_disk', return_value=True):
        controller.driver = MagicMock()
        controller.disk = MagicMock()
        controller.filesystem = MagicMock()
        # Configure filesystem.read_file to simulate the underlying error
        # This assumes read_file internally calls something like _find_path
        # which identifies it as a dir, causing read_file to fail.
        # Let's simulate the failure directly in read_file.
        controller.filesystem.read_file.side_effect = IsADirectoryError("Path is a directory")

        controller.open_disk("dummy.img", "image") # Call mocked open

        result = controller.read_file("/mydir")
        assert result is None # Controller should catch error and return None
        controller.filesystem.read_file.assert_called_once_with("/mydir")

def test_04_list_file_as_dir(error_controller):
    controller = error_controller
    file_path = "/mydir/myfile.txt"
    with patch.object(controller, 'open_disk', return_value=True):
        controller.driver = MagicMock()
        controller.disk = MagicMock()
        controller.filesystem = MagicMock()
        # Mock list_directory to simulate the underlying error
        controller.filesystem.list_directory.side_effect = NotADirectoryError(f"Path is not a directory: {file_path}")

        controller.open_disk("dummy.img", "image") # Call mocked open

        result = controller.list_directory(file_path)
        assert result == [], "Listing a file path should return empty list"
        controller.filesystem.list_directory.assert_called_once_with(file_path)
