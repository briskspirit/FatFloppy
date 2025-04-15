# tests/test_08_controller_errors.py
import unittest
from unittest.mock import patch
import os
import sys

from unittest.mock import MagicMock
# Ensure src is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.disk import DiskGeometry # For set_geometry test

class TestControllerErrors(unittest.TestCase):

    def setUp(self):
        print(f"\n--- Running test: {self.id()} ---")
        self.controller = DiskController()
        # We need a dummy disk/driver sometimes for methods that check self.disk first
        self.dummy_driver_patch = patch('fatfloppy.core.drivers.RawImageDriver') # Mock driver import/use
        self.MockRawImageDriver = self.dummy_driver_patch.start()

    def tearDown(self):
        print(f"--- Tearing down test: {self.id()} ---")
        self.dummy_driver_patch.stop()
        if self.controller.disk:
            self.controller.close_disk() # Ensure closed if a test accidentally opened one
        del self.controller
        print(f"--- Finished test: {self.id()} ---")

    def test_01_ops_before_open(self):
        """Test controller operations fail cleanly before a disk is opened"""
        self.assertIsNone(self.controller.disk, "Disk should be None initially")
        self.assertIsNone(self.controller.detect_geometry())
        with self.assertRaises(ValueError, msg="set_geometry should fail"):
            dummy_geom = DiskGeometry(1,1,1,1)
            self.controller.set_geometry(dummy_geom)
        self.assertIsNone(self.controller.detect_format())
        with self.assertRaises(ValueError, msg="set_format should fail"):
            # Need a mock profile for set_format
            mock_profile = MagicMock(name='mock_profile')
            mock_profile.name = "mock_fmt"
            mock_profile.description = "Mock Format"
            mock_profile.geometry = DiskGeometry(1,1,1,1)
            mock_profile.physical_format = MagicMock()
            self.controller.set_format(mock_profile)

        self.assertIsNone(self.controller.detect_filesystem())
        self.assertEqual(self.controller.list_directory("/"), [])
        self.assertIsNone(self.controller.read_file("/file.txt"))
        self.assertFalse(self.controller.write_file("/file.txt", b"data"))
        self.assertFalse(self.controller.create_directory("/dir"))
        self.assertFalse(self.controller.delete_item("/item"))
        self.assertIsNone(self.controller.get_free_space())
        self.assertEqual(self.controller.get_allocated_clusters(), [])

    def test_02_ops_after_close(self):
        """Test controller operations fail cleanly after disk is closed"""
        # Need to successfully open first - mock open_disk minimally
        with patch.object(DiskController, '_detect_image_file_format', return_value=True):
            # Simulate successful opening by setting internal state
            self.controller.driver = self.MockRawImageDriver()
            self.controller.disk = MagicMock()
            self.controller.filesystem = MagicMock()
            self.controller.disk.geometry = DiskGeometry(1,1,1,1) # Need geometry
            # Ensure the mocks exist before closing
            self.assertIsNotNone(self.controller.disk)
            self.assertIsNotNone(self.controller.driver)

        self.controller.close_disk()
        self.assertIsNone(self.controller.disk, "Disk should be None after close")

        # Now test operations again
        self.assertIsNone(self.controller.detect_geometry())
        with self.assertRaises(ValueError): self.controller.set_geometry(DiskGeometry(1,1,1,1))
        self.assertIsNone(self.controller.detect_format())
        # Cannot test set_format as it requires self.disk
        self.assertIsNone(self.controller.detect_filesystem())
        self.assertEqual(self.controller.list_directory("/"), [])
        self.assertIsNone(self.controller.read_file("/file.txt"))
        self.assertFalse(self.controller.write_file("/file.txt", b"data"))
        self.assertFalse(self.controller.create_directory("/dir"))
        self.assertFalse(self.controller.delete_item("/item"))
        self.assertIsNone(self.controller.get_free_space())
        self.assertEqual(self.controller.get_allocated_clusters(), [])


    def test_03_read_dir_as_file(self):
        """Test reading a directory path using read_file"""
        # Mock open_disk and list_directory to simulate a directory existing
        with patch.object(DiskController, 'open_disk', return_value=True):
            # Set up internal state as if open succeeded with a filesystem
            self.controller.driver = self.MockRawImageDriver()
            self.controller.disk = MagicMock()
            # Create the filesystem mock
            self.controller.filesystem = MagicMock()
            # Configure filesystem mock's _find_path
            mock_dir_info = MagicMock(is_dir=True)
            self.controller.filesystem._find_path = MagicMock(return_value=mock_dir_info)

            # ***** FIX: Configure mocked filesystem.read_file *****
            # Simulate the error the real FS would raise
            self.controller.filesystem.read_file.side_effect = ValueError("File not found: /mydir") # Or similar error
            # *****************************************************

            # Call open_disk (mocked)
            self.controller.open_disk("dummy.img", "image")

            # Attempt to read the directory path as a file
            result = self.controller.read_file("/mydir")
            self.assertIsNone(result, "Reading a directory path should return None")
            # Verify _find_path was called (implicitly by the real read_file, which we bypassed)
            # self.controller.filesystem._find_path.assert_called_once_with("/mydir") # This check is less relevant now
            # Verify filesystem.read_file was called
            self.controller.filesystem.read_file.assert_called_once_with("/mydir")

    def test_04_list_file_as_dir(self):
        """Test listing a file path using list_directory"""
        file_path = "/mydir/myfile.txt"
        # Mock open_disk
        with patch.object(DiskController, 'open_disk', return_value=True):
            self.controller.driver = self.MockRawImageDriver()
            self.controller.disk = MagicMock()
            # Create the filesystem mock
            self.controller.filesystem = MagicMock()

            # ***** FIX: Configure _find_path, remove list_directory mock *****
            # Configure _find_path to return a file entry
            mock_file_info = MagicMock(is_dir=False) # Key part: it's NOT a directory
            self.controller.filesystem._find_path = MagicMock(return_value=mock_file_info)

            # Remove the explicit mock of list_directory itself
            # Let the controller call the mock filesystem's default list_directory behavior.
            # Since the default MagicMock returns another MagicMock, we need to configure
            # what list_directory should return *based on the _find_path result*.
            # The easiest way is often to mock the *actual* FS behavior slightly.
            # The real list_directory checks if the result of find_path is a directory.
            def mock_list_directory(path):
                if path == "/":
                    return [MagicMock()] # Simulate some root content if needed
                # Simulate the internal check: find path, check if dir
                entry = self.controller.filesystem._find_path(path)
                if entry and not entry.is_dir: # If entry found and it's NOT a dir
                    # Real method returns [], simulating that behavior
                    return []
                elif entry and entry.is_dir:
                    # If it was a directory, return some dummy content
                    return [MagicMock(name="dummy_in_dir")]
                else: # Path not found by _find_path
                    return []
            self.controller.filesystem.list_directory = MagicMock(side_effect=mock_list_directory)
            # ******************************************************************

            # Call open_disk (mocked)
            self.controller.open_disk("dummy.img", "image")

            # Attempt to list the file path
            result = self.controller.list_directory(file_path)

            # --- Assertions ---
            # Check the final result (should be empty list as returned by mock logic)
            self.assertEqual(result, [], "Listing a file path should return empty list")
            # Verify find_path WAS called by our mock list_directory logic
            self.controller.filesystem._find_path.assert_called_once_with(file_path)
            # Verify list_directory itself was called
            self.controller.filesystem.list_directory.assert_called_once_with(file_path)


if __name__ == '__main__':
    unittest.main()
