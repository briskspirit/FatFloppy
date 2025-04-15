# tests/test_04_controller_image.py
import unittest
import os
import tempfile
import shutil

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.disk import DiskGeometry
from fatfloppy.core.format_definitions import FLOPPY_FORMATS

RESOURCE_DIR = os.path.join(os.path.dirname(__file__), 'resources')
EMPTY_IMG = os.path.join(RESOURCE_DIR, 'empty_1.44mb.img')
POPULATED_IMG = os.path.join(RESOURCE_DIR, 'populated_1.44mb.img')
TEST_FILE_TXT = os.path.join(RESOURCE_DIR, 'test_file.txt')
PATTERN_FILE_BIN = os.path.join(RESOURCE_DIR, 'pattern_file.bin')

FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']

class TestDiskControllerImage(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="fatfloppy_test_ctrl_")
        self.test_img_path = os.path.join(self.temp_dir, "test_ctrl_1.44mb.img")
        # Use populated image for read tests, empty for write tests if needed
        if not os.path.exists(POPULATED_IMG):
             raise unittest.SkipTest(f"{POPULATED_IMG} not found, cannot run controller read tests.")
        shutil.copy(POPULATED_IMG, self.test_img_path)

        self.controller = DiskController()
        print(f"\nRunning test: {self.id()}")

    def tearDown(self):
        self.controller.close_disk()
        del self.controller
        shutil.rmtree(self.temp_dir)
        print(f"Finished test: {self.id()}")

    def test_01_open_image_auto_detect_format(self):
        success = self.controller.open_disk(self.test_img_path, disk_type="image")
        self.assertTrue(success, "Failed to open image")
        self.assertIsNotNone(self.controller.disk)
        self.assertIsNotNone(self.controller.driver)
        self.assertIsNotNone(self.controller.disk.geometry, "Geometry should be detected")
        self.assertIsNotNone(self.controller.filesystem, "Filesystem should be detected")

        # Verify detected geometry (should match 1.44MB from BPB/detection)
        geom = self.controller.disk.geometry
        self.assertEqual(geom.cylinders, FMT_144.geometry.cylinders)
        self.assertEqual(geom.heads, FMT_144.geometry.heads)
        self.assertEqual(geom.sectors_per_track, FMT_144.geometry.sectors_per_track)
        self.assertEqual(geom.sector_size, FMT_144.geometry.sector_size)
        self.assertEqual(self.controller.filesystem.fat_type, "FAT12")


    def test_02_open_image_non_existent(self):
        success = self.controller.open_disk("/tmp/non_existent_image.img", disk_type="image")
        self.assertFalse(success, "Should fail to open non-existent image")
        self.assertIsNone(self.controller.disk)

    def test_03_close_disk(self):
        self.controller.open_disk(self.test_img_path, disk_type="image")
        self.assertIsNotNone(self.controller.disk)
        self.controller.close_disk()
        self.assertIsNone(self.controller.disk)
        self.assertIsNone(self.controller.driver)
        self.assertIsNone(self.controller.filesystem)

    def test_04_list_directory_root(self):
        # Assumes populated_1.44mb.img has DIR1 and maybe TEST.TXT at root
        self.controller.open_disk(self.test_img_path, disk_type="image")
        entries = self.controller.list_directory("/")
        self.assertGreater(len(entries), 0, "Populated image root directory is empty?")

        # Look for expected items (adjust based on your populated image)
        found_dir1 = any(e['name'] == 'DIR1' and e['is_dir'] for e in entries)
        # found_testtxt = any(e['name'] == 'TEST.TXT' and not e['is_dir'] for e in entries)

        self.assertTrue(found_dir1, "Expected 'DIR1' not found in root")
        # self.assertTrue(found_testtxt, "Expected 'TEST.TXT' not found in root")

    def test_05_list_subdirectory(self):
        # Assumes populated_1.44mb.img has DIR1/SUBDIR and maybe DIR1/FILE1.DAT
        self.controller.open_disk(self.test_img_path, disk_type="image")
        entries = self.controller.list_directory("/DIR1") # Use forward slash
        self.assertGreater(len(entries), 0, "Populated image DIR1 directory is empty?")

        found_subdir = any(e['name'] == 'SUBDIR' and e['is_dir'] for e in entries)
        # found_file1 = any(e['name'] == 'FILE1.DAT' and not e['is_dir'] for e in entries)

        self.assertTrue(found_subdir, "Expected 'SUBDIR' not found in DIR1")
        # self.assertTrue(found_file1, "Expected 'FILE1.DAT' not found in DIR1")


    def test_06_read_file(self):
         # Assumes populated_1.44mb.img has test_file.txt content in /DIR1/TEST.TXT
        filepath = "/DIR1/TEST.TXT" # Adjust path as needed
        self.controller.open_disk(self.test_img_path, disk_type="image")

        # Get expected content
        with open(TEST_FILE_TXT, "rb") as f:
            expected_content = f.read()

        read_content = self.controller.read_file(filepath)
        self.assertIsNotNone(read_content)
        self.assertEqual(read_content, expected_content)

    def test_07_read_non_existent_file(self):
        self.controller.open_disk(self.test_img_path, disk_type="image")
        read_content = self.controller.read_file("/NO/SUCH/FILE.XYZ")
        self.assertIsNone(read_content) # Controller returns None on error


    def test_08_write_new_file_and_verify(self):
        # Use the empty image for writing
        if not os.path.exists(EMPTY_IMG):
             raise unittest.SkipTest(f"{EMPTY_IMG} not found, cannot run controller write tests.")
        shutil.copy(EMPTY_IMG, self.test_img_path)

        self.controller.open_disk(self.test_img_path, disk_type="image")

        filepath = "/NEWFILE.DAT"
        test_content = b"Controller test write data \x00\xff\xfe"
        success = self.controller.write_file(filepath, test_content)
        self.assertTrue(success)

        # Read back immediately
        read_content = self.controller.read_file(filepath)
        self.assertEqual(read_content, test_content)

        # Close (flushes) and reopen
        self.controller.close_disk()
        reopen_success = self.controller.open_disk(self.test_img_path, disk_type="image")
        self.assertTrue(reopen_success)

        # Verify file exists and content is correct after reopen
        entries = self.controller.list_directory("/")
        self.assertTrue(any(e['name'] == 'NEWFILE.DAT' for e in entries))
        re_read_content = self.controller.read_file(filepath)
        self.assertEqual(re_read_content, test_content)


    def test_09_create_directory(self):
        if not os.path.exists(EMPTY_IMG): raise unittest.SkipTest(f"{EMPTY_IMG} not found.")
        shutil.copy(EMPTY_IMG, self.test_img_path)
        self.controller.open_disk(self.test_img_path, disk_type="image")

        dirpath = "/NEWDIR/SUB"
        success_create1 = self.controller.create_directory("/NEWDIR")
        self.assertTrue(success_create1)
        success_create2 = self.controller.create_directory(dirpath)
        self.assertTrue(success_create2)

        # Verify listings
        root_list = self.controller.list_directory("/")
        self.assertTrue(any(e['name'] == 'NEWDIR' and e['is_dir'] for e in root_list))
        newdir_list = self.controller.list_directory("/NEWDIR")
        self.assertTrue(any(e['name'] == 'SUB' and e['is_dir'] for e in newdir_list))

        # Write a file inside
        filepath = f"{dirpath}/TEST_IN_SUB.TXT"
        write_success = self.controller.write_file(filepath, b"hello")
        self.assertTrue(write_success)
        read_back = self.controller.read_file(filepath)
        self.assertEqual(read_back, b"hello")


    def test_10_delete_file_and_directory(self):
        if not os.path.exists(EMPTY_IMG): raise unittest.SkipTest(f"{EMPTY_IMG} not found.")
        shutil.copy(EMPTY_IMG, self.test_img_path)
        self.controller.open_disk(self.test_img_path, disk_type="image")

        dirpath = "/DELDIR"
        filepath = f"{dirpath}/DELFILE.TMP"

        self.controller.create_directory(dirpath)
        self.controller.write_file(filepath, b"to be deleted")

        # Verify existence
        self.assertTrue(any(e['name'] == 'DELDIR' for e in self.controller.list_directory("/")))
        self.assertTrue(any(e['name'] == 'DELFILE.TMP' for e in self.controller.list_directory(dirpath)))

        # Delete file
        del_file_success = self.controller.delete_item(filepath)
        self.assertTrue(del_file_success)
        self.assertFalse(any(e['name'] == 'DELFILE.TMP' for e in self.controller.list_directory(dirpath)))

        # Delete now-empty directory
        del_dir_success = self.controller.delete_item(dirpath)
        self.assertTrue(del_dir_success)
        self.assertFalse(any(e['name'] == 'DELDIR' for e in self.controller.list_directory("/")))

    def test_11_get_free_space(self):
        # Use populated image
        self.controller.open_disk(self.test_img_path, disk_type="image")
        space_info = self.controller.get_free_space()
        self.assertIsNotNone(space_info)
        free_bytes, total_bytes = space_info

        self.assertEqual(total_bytes, FMT_144.geometry.total_bytes)
        self.assertLess(free_bytes, total_bytes) # Populated should have used space
        self.assertGreater(free_bytes, 0)

    def test_12_get_allocated_clusters(self):
        # Use populated image
        self.controller.open_disk(self.test_img_path, disk_type="image")
        clusters = self.controller.get_allocated_clusters()
        self.assertIsNotNone(clusters)
        self.assertIsInstance(clusters, list)
        self.assertGreater(len(clusters), 0) # Populated image must have allocated clusters
        self.assertTrue(all(isinstance(c, int) for c in clusters))


if __name__ == '__main__':
    unittest.main()
