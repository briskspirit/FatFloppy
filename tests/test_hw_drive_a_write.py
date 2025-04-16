# tests/test_hw_drive_a_write.py
import unittest
import os
import sys
import time

# Ensure src is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.format_definitions import FLOPPY_FORMATS

# --- Test Configuration ---
TEST_HW_ENABLED = os.getenv('TEST_HW', 'false').lower() == 'true'
TARGET_DRIVE = 'A'
TARGET_FORMAT_KEY = '1.44M'
TARGET_DRIVE_SIZE = "3.5"
FMT_PROFILE = FLOPPY_FORMATS['ibm_3.5_1.44m']

@unittest.skipUnless(TEST_HW_ENABLED, "Hardware tests disabled (TEST_HW not 'true')")
@unittest.skipUnless(os.getenv('TEST_DRIVE') == TARGET_DRIVE and os.getenv('TEST_FORMAT') == TARGET_FORMAT_KEY,
                    f"Skipping: Requires TEST_DRIVE={TARGET_DRIVE} and TEST_FORMAT={TARGET_FORMAT_KEY}")
class TestHardwareDriveAWrite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        print(f"\n--- Setting up Hardware Test Class for Drive {TARGET_DRIVE} ({TARGET_FORMAT_KEY}) ---")
        print("INFO: Ensure the PREPARED 'Write Test' (empty formatted) disk for Drive A (1.44M) is inserted.")
        print("      This disk WILL BE MODIFIED.")
        cls.controller = DiskController()
        gw_device = os.environ.get('GW_DEVICE', None)
        print(f"Attempting to open Drive {TARGET_DRIVE}...")
        # Open with explicit format to ensure correct parameters for writing/formatting
        format_info_dict = {
            **{k: v for k, v in FMT_PROFILE.geometry.__dict__.items() if not k.startswith('_')},
            **{k: v for k, v in FMT_PROFILE.physical_format.__dict__.items() if not k.startswith('_')}
        }
        success = cls.controller.open_disk(
            source=gw_device,
            disk_type="physical",
            drive_letter=TARGET_DRIVE,
            drive_size=TARGET_DRIVE_SIZE,
            format_info=format_info_dict # Use explicit format for writing
        )
        if not success:
             cls.controller.close_disk()
             raise unittest.SkipTest(f"Failed to open physical drive {TARGET_DRIVE}. Check connection and disk.")
        # Filesystem might be None if disk is perfectly empty, but open should succeed
        # if not cls.controller.filesystem or not cls.controller.filesystem.is_valid():
        #      cls.controller.close_disk()
        #      raise unittest.SkipTest(f"No valid filesystem detected on drive {TARGET_DRIVE}.")
        print(f"Drive {TARGET_DRIVE} opened successfully.")

    @classmethod
    def tearDownClass(cls):
        print(f"\n--- Tearing down Hardware Test Class for Drive {TARGET_DRIVE} ---")
        if hasattr(cls, 'controller') and cls.controller:
            cls.controller.close_disk()

    # Helper to clean up created items
    def _cleanup_item(self, path):
        print(f"Attempting cleanup: delete '{path}'")
        try:
            # Check existence before deleting
            parent, name = self.controller.filesystem._split_path(path)
            parent_cluster = self.controller.filesystem._get_directory_cluster(parent)
            try:
                 entry, _ = self.controller.filesystem._find_entry_in_directory(parent_cluster, name)
                 # Now delete
                 deleted = self.controller.delete_item(path)
                 if not deleted:
                      print(f"WARN: Cleanup delete failed for {path}")
            except FileNotFoundError:
                 print(f"Cleanup: Item '{path}' not found, likely already deleted.")

        except Exception as e:
            print(f"WARN: Exception during cleanup of {path}: {e}")

    def test_01_hw_A_create_write_read_delete_file(self):
        """HW Write: Create, write, read, delete file on Drive A"""
        print(f"Running: {self.id()}")
        filepath = "/HW_WRITE.TXT"
        content = b"Test data written to hardware " * 5 # Small file
        try:
            # Write
            print(f"  Writing {filepath}...")
            write_ok = self.controller.write_file(filepath, content)
            self.assertTrue(write_ok, f"write_file failed for {filepath}")

            # Read back
            print(f"  Reading back {filepath}...")
            read_content = self.controller.read_file(filepath)
            self.assertEqual(read_content, content, f"Read content mismatch for {filepath}")

        finally:
            # Delete
            self.addCleanup(self._cleanup_item, filepath)


    def test_02_hw_A_create_delete_dir(self):
        """HW Write: Create and delete directory on Drive A"""
        print(f"Running: {self.id()}")
        dirpath = "/HW_DIR"
        try:
            # Create
            print(f"  Creating {dirpath}...")
            create_ok = self.controller.create_directory(dirpath)
            self.assertTrue(create_ok, f"create_directory failed for {dirpath}")

            # Verify listing
            root_list = self.controller.list_directory("/")
            self.assertTrue(any(e['name'] == 'HW_DIR' and e['is_dir'] for e in root_list), f"{dirpath} not found after creation")

        finally:
            # Delete
            self.addCleanup(self._cleanup_item, dirpath)

    def test_03_hw_A_write_multicluster_file(self):
        """HW Write: Create file spanning multiple clusters on Drive A"""
        print(f"Running: {self.id()}")
        filepath = "/MULTCLUS.BIN"
        # 1.44MB, 1 sector/cluster = 512 bytes/cluster. Write > 512 bytes.
        content = b"ClusterData" * 150 # Approx 1650 bytes -> 4 clusters
        self.assertGreater(len(content), 512 * 3)
        try:
            # Write
            print(f"  Writing {filepath} ({len(content)} bytes)...")
            write_ok = self.controller.write_file(filepath, content)
            self.assertTrue(write_ok, f"write_file failed for {filepath}")

            # Read back
            print(f"  Reading back {filepath}...")
            read_content = self.controller.read_file(filepath)
            self.assertEqual(read_content, content, f"Read content mismatch for {filepath}")

            # Check allocation (optional but good)
            alloc_clusters = self.controller.get_allocated_clusters()
            found = False
            for item in self.controller.list_directory("/"):
                if item['name'] == 'MULTCLUS.BIN':
                    # Calculate expected clusters
                    expected_clusters = (len(content) + self.controller.filesystem.cluster_size - 1) // self.controller.filesystem.cluster_size
                    # Need to get cluster chain length, get_allocated_clusters is total
                    # cluster_chain = self.controller.filesystem._get_cluster_chain(item['starting_cluster'])
                    # self.assertEqual(len(cluster_chain), expected_clusters)
                    found = True
                    break
            self.assertTrue(found)


        finally:
            # Delete
            self.addCleanup(self._cleanup_item, filepath)


if __name__ == '__main__':
    unittest.main()
