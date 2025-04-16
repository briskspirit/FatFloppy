# tests/test_hw_drive_a_read.py
import unittest
import os
import sys

# Ensure src is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.format_definitions import FLOPPY_FORMATS

# --- Test Configuration ---
TEST_HW_ENABLED = os.getenv('TEST_HW', 'false').lower() == 'true'
TARGET_DRIVE = 'A'
TARGET_FORMAT_KEY = '1.44M'
TARGET_DRIVE_SIZE = "3.5"
FMT_PROFILE = FLOPPY_FORMATS['ibm_3.5_1.44m'] # Expected format

# Path to resource files for content verification
RESOURCE_DIR = os.path.join(os.path.dirname(__file__), 'resources')
TEST_FILE_TXT_PATH = os.path.join(RESOURCE_DIR, 'TEST.TXT')
PATTERN_FILE_BIN_PATH = os.path.join(RESOURCE_DIR, 'PATTERN.BIN')


@unittest.skipUnless(TEST_HW_ENABLED, "Hardware tests disabled (TEST_HW not 'true')")
@unittest.skipUnless(os.getenv('TEST_DRIVE') == TARGET_DRIVE and os.getenv('TEST_FORMAT') == TARGET_FORMAT_KEY,
                    f"Skipping: Requires TEST_DRIVE={TARGET_DRIVE} and TEST_FORMAT={TARGET_FORMAT_KEY}")
class TestHardwareDriveARead(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        print(f"\n--- Setting up Hardware Test Class for Drive {TARGET_DRIVE} ({TARGET_FORMAT_KEY}) ---")
        print("INFO: Ensure the PREPARED 'Read Test' disk for Drive A (1.44M) with specified files is inserted.")
        cls.controller = DiskController()
        gw_device = os.environ.get('GW_DEVICE', None)
        print(f"Attempting to open Drive {TARGET_DRIVE}...")
        # Let it auto-detect format from the prepared disk
        success = cls.controller.open_disk(
            source=gw_device,
            disk_type="physical",
            drive_letter=TARGET_DRIVE,
            drive_size=TARGET_DRIVE_SIZE
        )
        if not success:
            cls.controller.close_disk() # Clean up controller if open failed
            raise unittest.SkipTest(f"Failed to open physical drive {TARGET_DRIVE}. Check connection and disk.")
        if not cls.controller.filesystem or not cls.controller.filesystem.is_valid():
             cls.controller.close_disk()
             raise unittest.SkipTest(f"No valid filesystem detected on drive {TARGET_DRIVE}.")
        print(f"Drive {TARGET_DRIVE} opened successfully.")
        # Load expected content
        try:
             with open(TEST_FILE_TXT_PATH, "rb") as f: cls.expected_test_txt = f.read()
             with open(PATTERN_FILE_BIN_PATH, "rb") as f: cls.expected_pattern_bin = f.read()
        except FileNotFoundError as e:
             raise unittest.SkipTest(f"Resource file not found: {e}. Cannot run content verification.")


    @classmethod
    def tearDownClass(cls):
        print(f"\n--- Tearing down Hardware Test Class for Drive {TARGET_DRIVE} ---")
        if hasattr(cls, 'controller') and cls.controller:
            cls.controller.close_disk()

    def test_01_hw_A_list_root(self):
        """HW Read: List root directory of Drive A"""
        print(f"Running: {self.id()}")
        entries = self.controller.list_directory("/")
        self.assertGreaterEqual(len(entries), 2, "Root directory has fewer than expected items") # TEST.TXT, DIR1
        root_names = {e['name'].upper() for e in entries}
        self.assertIn('TEST.TXT', root_names)
        self.assertIn('DIR1', root_names)
        # Check types
        self.assertFalse(next(e for e in entries if e['name'] == 'TEST.TXT')['is_dir'])
        self.assertTrue(next(e for e in entries if e['name'] == 'DIR1')['is_dir'])
        print(f"Found {len(entries)} items in root.")

    def test_02_hw_A_list_dir1(self):
        """HW Read: List /DIR1 directory on Drive A"""
        print(f"Running: {self.id()}")
        entries = self.controller.list_directory("/DIR1")
        self.assertGreaterEqual(len(entries), 3, "/DIR1 has fewer than expected items") # PATTERN.BIN, TEST.TXT, SUBDIR
        dir1_names = {e['name'].upper() for e in entries}
        self.assertIn('PATTERN.BIN', dir1_names)
        self.assertIn('TEST.TXT', dir1_names)
        self.assertIn('SUBDIR', dir1_names)
        # Check types
        self.assertFalse(next(e for e in entries if e['name'] == 'PATTERN.BIN')['is_dir'])
        self.assertFalse(next(e for e in entries if e['name'] == 'TEST.TXT')['is_dir'])
        self.assertTrue(next(e for e in entries if e['name'] == 'SUBDIR')['is_dir'])
        print(f"Found {len(entries)} items in /DIR1.")

    def test_03_hw_A_list_subdir(self):
        """HW Read: List /DIR1/SUBDIR directory on Drive A"""
        print(f"Running: {self.id()}")
        entries = self.controller.list_directory("/DIR1/SUBDIR")
        self.assertGreaterEqual(len(entries), 1, "/DIR1/SUBDIR has fewer than expected items") # TEST.TXT
        subdir_names = {e['name'].upper() for e in entries}
        self.assertIn('TEST.TXT', subdir_names)
        # Check types
        self.assertFalse(next(e for e in entries if e['name'] == 'TEST.TXT')['is_dir'])
        print(f"Found {len(entries)} items in /DIR1/SUBDIR.")

    def test_04_hw_A_read_root_file(self):
        """HW Read: Read /TEST.TXT content on Drive A"""
        print(f"Running: {self.id()}")
        filepath = "/TEST.TXT"
        content = self.controller.read_file(filepath)
        self.assertIsNotNone(content, f"Failed to read {filepath}")
        self.assertGreater(len(content), 0, f"{filepath} is empty")
        # Optionally compare with expected content if TEST.TXT content is fixed
        # self.assertEqual(content, self.expected_text_txt)
        print(f"Read {len(content)} bytes from {filepath}")

    def test_05_hw_A_read_dir1_files(self):
        """HW Read: Read files in /DIR1 on Drive A"""
        print(f"Running: {self.id()}")
        # Read PATTERN.BIN
        filepath_bin = "/DIR1/PATTERN.BIN"
        content_bin = self.controller.read_file(filepath_bin)
        self.assertIsNotNone(content_bin, f"Failed to read {filepath_bin}")
        self.assertEqual(content_bin, self.expected_pattern_bin, f"Content mismatch for {filepath_bin}")
        print(f"Read {len(content_bin)} bytes from {filepath_bin}")
        # Read TEST.TXT
        filepath_txt = "/DIR1/TEST.TXT"
        content_txt = self.controller.read_file(filepath_txt)
        self.assertIsNotNone(content_txt, f"Failed to read {filepath_txt}")
        self.assertEqual(content_txt, self.expected_test_txt, f"Content mismatch for {filepath_txt}")
        print(f"Read {len(content_txt)} bytes from {filepath_txt}")

    def test_06_hw_A_read_subdir_file(self):
        """HW Read: Read file in /DIR1/SUBDIR on Drive A"""
        print(f"Running: {self.id()}")
        filepath = "/DIR1/SUBDIR/TEST.TXT"
        content = self.controller.read_file(filepath)
        self.assertIsNotNone(content, f"Failed to read {filepath}")
        self.assertEqual(content, self.expected_test_txt, f"Content mismatch for {filepath}")
        print(f"Read {len(content)} bytes from {filepath}")

    def test_07_hw_A_get_disk_info(self):
        """HW Read: Get free space and allocated clusters on Drive A"""
        print(f"Running: {self.id()}")
        free, total = self.controller.get_free_space()
        clusters = self.controller.get_allocated_clusters()
        self.assertIsNotNone(free); self.assertIsNotNone(total); self.assertIsNotNone(clusters)
        self.assertLess(free, total)
        self.assertEqual(len(clusters), 7, "Expected more allocated clusters for populated disk")
        print(f"Disk Info: Free={free/1024:.1f}KB, Total={total/1024:.1f}KB, Clusters={len(clusters)}")

if __name__ == '__main__':
    unittest.main()
