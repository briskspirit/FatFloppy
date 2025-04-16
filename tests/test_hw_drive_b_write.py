# tests/test_hw_drive_b_write.py
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
TARGET_DRIVE = 'B'
TARGET_FORMAT_KEY = '360K'
TARGET_DRIVE_SIZE = "5.25"
FMT_PROFILE = FLOPPY_FORMATS['ibm_5.25_360k']

@unittest.skipUnless(TEST_HW_ENABLED, "Hardware tests disabled (TEST_HW not 'true')")
@unittest.skipUnless(os.getenv('TEST_DRIVE') == TARGET_DRIVE and os.getenv('TEST_FORMAT') == TARGET_FORMAT_KEY,
                    f"Skipping: Requires TEST_DRIVE={TARGET_DRIVE} and TEST_FORMAT={TARGET_FORMAT_KEY}")
class TestHardwareDriveBWrite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        print(f"\n--- Setting up Hardware Test Class for Drive {TARGET_DRIVE} ({TARGET_FORMAT_KEY}) ---")
        print("INFO: Ensure the PREPARED 'Write Test' (empty formatted) disk for Drive B (360K) is inserted.")
        print("      This disk WILL BE MODIFIED.")
        cls.controller = DiskController()
        gw_device = os.environ.get('GW_DEVICE', None)
        print(f"Attempting to open Drive {TARGET_DRIVE}...")
        format_info_dict = {
            **{k: v for k, v in FMT_PROFILE.geometry.__dict__.items() if not k.startswith('_')},
            **{k: v for k, v in FMT_PROFILE.physical_format.__dict__.items() if not k.startswith('_')}
        }
        success = cls.controller.open_disk(
            source=gw_device,
            disk_type="physical",
            drive_letter=TARGET_DRIVE,
            drive_size=TARGET_DRIVE_SIZE,
            format_info=format_info_dict
        )
        if not success:
             cls.controller.close_disk()
             raise unittest.SkipTest(f"Failed to open physical drive {TARGET_DRIVE}. Check connection and disk.")
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
                 deleted = self.controller.delete_item(path)
                 if not deleted: print(f"WARN: Cleanup delete failed for {path}")
            except FileNotFoundError:
                 print(f"Cleanup: Item '{path}' not found.")
        except Exception as e:
            print(f"WARN: Exception during cleanup of {path}: {e}")

    def test_01_hw_B_create_write_read_delete_file(self):
        """HW Write: Create, write, read, delete file on Drive B"""
        print(f"Running: {self.id()}")
        filepath = "/WRITE_B.TMP"
        content = b"360KB hardware test " * 10
        try:
            print(f"  Writing {filepath}...")
            write_ok = self.controller.write_file(filepath, content)
            self.assertTrue(write_ok, f"write_file failed for {filepath}")
            print(f"  Reading back {filepath}...")
            read_content = self.controller.read_file(filepath)
            self.assertEqual(read_content, content, f"Read content mismatch for {filepath}")
        finally:
            self.addCleanup(self._cleanup_item, filepath)

    def test_02_hw_B_create_delete_dir(self):
        """HW Write: Create and delete directory on Drive B"""
        print(f"Running: {self.id()}")
        dirpath = "/B_DIR"
        try:
            print(f"  Creating {dirpath}...")
            create_ok = self.controller.create_directory(dirpath)
            self.assertTrue(create_ok, f"create_directory failed for {dirpath}")
            root_list = self.controller.list_directory("/")
            self.assertTrue(any(e['name'] == 'B_DIR' and e['is_dir'] for e in root_list), f"{dirpath} not found")
        finally:
            self.addCleanup(self._cleanup_item, dirpath)

    def test_03_hw_B_write_multicluster_file(self):
        """HW Write: Create file spanning multiple clusters on Drive B"""
        print(f"Running: {self.id()}")
        filepath = "/MULTI_B.BIN"
        # 360KB, 2 sectors/cluster = 1024 bytes/cluster. Write > 1024*2 bytes for 3 clusters.
        content = b"360K Cluster " * 160 # Approx 2100 bytes -> 3 clusters
        self.assertGreater(len(content), 1024 * 2, "Generated content length is not > 2 clusters")
        try:
            print(f"  Writing {filepath} ({len(content)} bytes)...")
            write_ok = self.controller.write_file(filepath, content)
            self.assertTrue(write_ok, f"write_file failed for {filepath}")
            print(f"  Reading back {filepath}...")
            read_content = self.controller.read_file(filepath)
            self.assertEqual(read_content, content, f"Read content mismatch for {filepath}")
        finally:
            self.addCleanup(self._cleanup_item, filepath)


if __name__ == '__main__':
    unittest.main()
