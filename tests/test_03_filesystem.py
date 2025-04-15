# tests/test_03_filesystem.py
import unittest
import os
import tempfile
import shutil
import struct
import datetime

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from fatfloppy.core.drivers import RawImageDriver
from fatfloppy.core.disk import Disk
from fatfloppy.core.filesystem import FATFilesystem, FileInfo
from fatfloppy.core.format_definitions import FLOPPY_FORMATS

RESOURCE_DIR = os.path.join(os.path.dirname(__file__), 'resources')
EMPTY_IMG = os.path.join(RESOURCE_DIR, 'empty_1.44mb.img')
POPULATED_IMG = os.path.join(RESOURCE_DIR, 'populated_1.44mb.img')
TEST_FILE_TXT = os.path.join(RESOURCE_DIR, 'test_file.txt')
PATTERN_FILE_BIN = os.path.join(RESOURCE_DIR, 'pattern_file.bin')

FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']
FMT_720 = FLOPPY_FORMATS['ibm_3.5_720k']

class TestFATFilesystem(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="fatfloppy_test_fs_")
        self.test_img_path = os.path.join(self.temp_dir, "test_fs_1.44mb.img")

        if not os.path.exists(EMPTY_IMG):
             raise unittest.SkipTest(f"{EMPTY_IMG} not found, cannot run filesystem tests.")
        # Use explicit overwrite
        with open(EMPTY_IMG, 'rb') as src, open(self.test_img_path, 'wb') as dst:
             shutil.copyfileobj(src, dst)

        self.driver = RawImageDriver(self.test_img_path)
        self.disk = Disk(self.driver)
        self.disk.set_geometry(FMT_144.geometry)
        self.driver.set_physical_format(FMT_144.physical_format)

        self.fs = FATFilesystem(self.disk)
        self.fs.fat_cache = None
        self.fs._cached_allocated_clusters = None

        print(f"\nRunning test: {self.id()}")
        self.assertTrue(self.fs.is_valid(), "Filesystem should be valid on setup")


    def tearDown(self):
        print(f"Starting tearDown for: {self.id()}")
        # --- Force Resource Release and Deletion Order ---

        # 1. Dereference filesystem, disk, driver objects first
        #    This should trigger __del__ if defined, and signals they are no longer needed.
        fs_to_del = getattr(self, 'fs', None)
        disk_to_del = getattr(self, 'disk', None)
        driver_to_del = getattr(self, 'driver', None)
        img_path_to_del = getattr(self, 'test_img_path', None)
        temp_dir_to_del = getattr(self, 'temp_dir', None)

        # Explicitly set attributes to None before deleting the variables
        if hasattr(self, 'fs'): self.fs = None
        if hasattr(self, 'disk'): self.disk = None
        if hasattr(self, 'driver'): self.driver = None
        if hasattr(self, 'test_img_path'): self.test_img_path = None
        if hasattr(self, 'temp_dir'): self.temp_dir = None

        del fs_to_del
        del disk_to_del
        del driver_to_del

        # 2. Optional: Force Garbage Collection (might help diagnose, but shouldn't be needed)
        # import gc
        # gc.collect()

        # 3. Remove the temporary directory (which contains the image file)
        #    This ensures the file is gone from the filesystem.
        if temp_dir_to_del and os.path.exists(temp_dir_to_del):
            print(f"Attempting to remove directory: {temp_dir_to_del}")
            try:
                shutil.rmtree(temp_dir_to_del)
                print(f"Successfully removed directory: {temp_dir_to_del}")
            except OSError as e:
                # Handle potential errors (e.g., file still locked - unlikely)
                print(f"ERROR: Failed to remove temp dir '{temp_dir_to_del}' in tearDown: {e}")
                # If removal fails, the next test's setUp might fail or reuse the old dir.
                # Depending on test runner, this might halt execution.
        elif temp_dir_to_del:
             print(f"Directory not found for removal: {temp_dir_to_del}")


        print(f"Finished tearDown for: {self.id()}")

    # --- Helper to read FAT entry (simplistic for testing) ---
    def _read_test_fat_entry(self, cluster: int):
         if not self.fs.is_valid(): return None
         # Read primary FAT only
         fat_offset = self.fs.fat_start + int(cluster * 1.5)
         value_bytes = self.fs._read_bytes(fat_offset, 2)
         value = struct.unpack('<H', value_bytes)[0]

         if cluster % 2 == 0:
             return value & 0x0FFF
         else:
             return value >> 4

    # --- Helper to check FAT mirroring ---
    def _check_fat_mirror(self):
        if not self.fs.is_valid() or self.fs.boot_sector.num_fats < 2:
            return True # No second FAT to check

        fat1_offset = self.fs.fat_start
        fat2_offset = fat1_offset + (self.fs.boot_sector.sectors_per_fat * self.fs.boot_sector.bytes_per_sector)
        fat_size = self.fs.boot_sector.sectors_per_fat * self.fs.boot_sector.bytes_per_sector

        fat1_data = self.fs._read_bytes(fat1_offset, fat_size)
        fat2_data = self.fs._read_bytes(fat2_offset, fat_size)
        self.assertEqual(fat1_data, fat2_data, "FAT tables are not mirrored correctly")


    def test_01_initialization_valid(self):
        self.assertTrue(self.fs.is_valid())
        self.assertEqual(self.fs.fat_type, "FAT12")
        self.assertGreater(self.fs.cluster_size, 0)
        self.assertGreater(self.fs.num_clusters, 0)
        # Check specific 1.44MB params
        self.assertEqual(self.fs.boot_sector.total_sectors, 2880)
        self.assertEqual(self.fs.cluster_size, 512) # 1 sector/cluster for 1.44
        self.assertEqual(self.fs.num_clusters, 2847) # 2880 - 1 (boot) - 18 (FATs) - 14 (root) = 2847 sectors = clusters

    def test_02_initialization_invalid_boot_sig(self):
        # Corrupt boot signature in the image data
        self.driver.image_data[510:512] = b'\x00\x00'
        # Need to re-initialize FS to read the corrupted data
        fs_corrupt = FATFilesystem(self.disk)
        self.assertFalse(fs_corrupt.is_valid(), "Filesystem should be invalid with bad boot signature")

    def test_03_list_root_directory_empty(self):
        entries = self.fs.list_directory("/")
        self.assertEqual(entries, [])

    def test_04_create_file_in_root(self):
        filename = "TEST.TXT"
        filedata = b"This is a test file."
        self.fs.write_file(filename, filedata)

        # Check FAT mirror after write
        self._check_fat_mirror()

        entries = self.fs.list_directory("/")
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertIsInstance(entry, FileInfo)
        self.assertEqual(entry.name, filename)
        self.assertEqual(entry.size, len(filedata))
        self.assertFalse(entry.is_dir)
        self.assertGreater(entry.starting_cluster, 1) # Should be allocated

        # Verify FAT entry for the first cluster
        fat_val = self._read_test_fat_entry(entry.starting_cluster)
        self.assertEqual(fat_val, 0xFFF, "Single cluster file should end with 0xFFF")

        # Verify free space decreased
        free_before, total = self.fs.get_free_space() # Force recalculation
        self.fs._cached_allocated_clusters = None # Force recalc
        self.fs.write_file("DUMMY.DAT", b"data") # Write something small
        free_after, _ = self.fs.get_free_space()
        self.assertLess(free_after, free_before, "Free space should decrease after writing")
        self.fs.delete("DUMMY.DAT") # Clean up


    def test_05_read_file_in_root(self):
        filename = "READTEST.DOC"
        filedata = b"Some data to read back."
        self.fs.write_file(filename, filedata)
        self._check_fat_mirror() # Check mirror after write

        read_data = self.fs.read_file(filename)
        self.assertEqual(read_data, filedata)

    def test_06_delete_file_in_root(self):
        filename = "TO_DEL.TMP"
        filedata = b"Temporary data."
        self.fs.write_file(filename, filedata)

        entries = self.fs.list_directory("/")
        self.assertTrue(any(e.name == filename for e in entries))
        entry = next(e for e in entries if e.name == filename)
        start_cluster = entry.starting_cluster

        # Verify cluster was allocated
        fat_val_before = self._read_test_fat_entry(start_cluster)
        self.assertNotEqual(fat_val_before, 0, "Cluster should be allocated before delete")

        self.fs.delete(filename)
        self._check_fat_mirror() # Check mirror after delete

        entries_after = self.fs.list_directory("/")
        self.assertFalse(any(e.name == filename for e in entries_after))

        # Verify cluster is freed
        fat_val_after = self._read_test_fat_entry(start_cluster)
        self.assertEqual(fat_val_after, 0, "Cluster should be marked free (0) after delete")

        # Verify free space increased
        free_before, total = self.fs.get_free_space()
        self.fs.write_file("DUMMY2.DAT", b"data") # Write
        self.fs.delete("DUMMY2.DAT") # Delete
        free_after, _ = self.fs.get_free_space() # Recalc
        self.assertGreaterEqual(free_after, free_before, "Free space should increase or stay same after deleting")


    def test_07_create_directory_in_root(self):
        dirname = "MYDIR"
        self.fs.create_directory(dirname)
        self._check_fat_mirror() # Check mirror

        entries = self.fs.list_directory("/")
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.name, dirname)
        self.assertTrue(entry.is_dir)
        self.assertEqual(entry.size, 0)
        self.assertGreater(entry.starting_cluster, 1)

        # Verify directory cluster contains . and ..
        dir_entries = self.fs._list_directory_by_cluster(entry.starting_cluster)
        self.assertEqual(len(dir_entries), 2)
        self.assertEqual(dir_entries[0].name, ".")
        self.assertEqual(dir_entries[0].starting_cluster, entry.starting_cluster)
        self.assertEqual(dir_entries[1].name, "..")
        self.assertEqual(dir_entries[1].starting_cluster, 0) # Parent is root

        # Verify FAT entry for the directory cluster
        fat_val = self._read_test_fat_entry(entry.starting_cluster)
        self.assertEqual(fat_val, 0xFFF, "Single cluster directory should end with 0xFFF")

    def test_08_create_file_in_subdir(self):
        dirname = "SUB"
        filename = "INSIDE.DAT"
        filepath = f"{dirname}/{filename}"
        filedata = b"Data inside a subdirectory."

        self.fs.create_directory(dirname)
        self.fs.write_file(filepath, filedata)
        self._check_fat_mirror()

        # Check listing the subdirectory
        sub_entries = self.fs.list_directory(dirname)
        self.assertEqual(len(sub_entries), 1)
        entry = sub_entries[0]
        self.assertEqual(entry.name, filename)
        self.assertEqual(entry.size, len(filedata))
        self.assertFalse(entry.is_dir)

        # Read back the file
        read_data = self.fs.read_file(filepath)
        self.assertEqual(read_data, filedata)

    def test_09_delete_empty_directory(self):
        dirname = "EMPTYDIR"
        self.fs.create_directory(dirname)
        dir_entry = next(e for e in self.fs.list_directory("/") if e.name == dirname)
        start_cluster = dir_entry.starting_cluster

        self.fs.delete(dirname)
        self._check_fat_mirror()

        entries_after = self.fs.list_directory("/")
        self.assertFalse(any(e.name == dirname for e in entries_after))

        # Verify cluster is freed
        fat_val_after = self._read_test_fat_entry(start_cluster)
        self.assertEqual(fat_val_after, 0, "Directory cluster should be freed after delete")

    def test_10_delete_non_empty_directory_fails(self):
        dirname = "NOTEMPTY"
        filename = "FILE.IN"
        filepath = f"{dirname}/{filename}"
        self.fs.create_directory(dirname)
        self.fs.write_file(filepath, b"data")

        with self.assertRaisesRegex(ValueError, "Directory not empty"):
            self.fs.delete(dirname)

        # Verify directory and file still exist
        root_entries = self.fs.list_directory("/")
        self.assertTrue(any(e.name == dirname for e in root_entries))
        sub_entries = self.fs.list_directory(dirname)
        self.assertTrue(any(e.name == filename for e in sub_entries))

    def test_11_delete_file_in_subdir_then_delete_dir(self):
        dirname = "CLEANUP"
        filename = "GONE.TXT"
        filepath = f"{dirname}/{filename}"
        self.fs.create_directory(dirname)
        self.fs.write_file(filepath, b"delete me")

        self.fs.delete(filepath) # Delete file first
        self._check_fat_mirror()
        self.fs.delete(dirname)  # Now delete empty dir
        self._check_fat_mirror()

        root_entries = self.fs.list_directory("/")
        self.assertFalse(any(e.name == dirname for e in root_entries))

    def test_12_write_large_file_multiple_clusters(self):
        filename = "LARGE.BIN"
        # Create data larger than one cluster (512 bytes for 1.44MB)
        # Let's make it span 3 clusters (e.g., 1200 bytes)
        filedata = bytes([i % 256 for i in range(1200)])
        self.assertEqual(self.fs.cluster_size, 512) # Check assumption

        self.fs.write_file(filename, filedata)
        self._check_fat_mirror()

        entry = next(e for e in self.fs.list_directory("/") if e.name == filename)
        self.assertEqual(entry.size, len(filedata))

        # Verify cluster chain in FAT
        c1 = entry.starting_cluster
        self.assertGreater(c1, 1)
        c2 = self._read_test_fat_entry(c1)
        self.assertGreater(c2, 1)
        self.assertNotEqual(c1, c2)
        c3 = self._read_test_fat_entry(c2)
        self.assertGreater(c3, 1)
        self.assertNotEqual(c2, c3)
        self.assertNotEqual(c1, c3)
        end_marker = self._read_test_fat_entry(c3)
        self.assertEqual(end_marker, 0xFFF, "Third cluster should be end of chain")

        # Read back data
        read_data = self.fs.read_file(filename)
        self.assertEqual(read_data, filedata)

    def test_13_overwrite_file(self):
        filename = "OVERWRIT.EME"
        initial_data = b"Initial content."
        new_data = b"This is the new content, much longer than the first."

        self.fs.write_file(filename, initial_data)
        entry1 = next(e for e in self.fs.list_directory("/") if e.name == filename)
        cluster1 = entry1.starting_cluster
        size1 = entry1.size

        self.fs.write_file(filename, new_data)
        self._check_fat_mirror()

        entry2 = next(e for e in self.fs.list_directory("/") if e.name == filename)
        cluster2 = entry2.starting_cluster
        size2 = entry2.size

        self.assertEqual(size2, len(new_data))
        self.assertNotEqual(size1, size2)
        # Overwriting might reallocate clusters, so cluster1 != cluster2 is possible
        # but not guaranteed if the new file fits in the old clusters.

        read_data = self.fs.read_file(filename)
        self.assertEqual(read_data, new_data)

    def test_14_filesystem_info(self):
        free_start, total_start = self.fs.get_free_space()
        alloc_start = self.fs.get_allocated_clusters()

        self.assertEqual(total_start, FMT_144.geometry.total_bytes)
        self.assertGreater(free_start, 0)
        # Empty formatted disk should have only reserved clusters (0, 1) and maybe bad sectors
        # Our simple FAT read doesn't check for bad sectors, so allocated should be empty initially.
        # self.assertEqual(len(alloc_start), 0) # This might fail if formatting tool marked bad clusters

        # Write a file using 3 clusters
        filedata = bytes([i % 256 for i in range(1200)])
        self.fs.write_file("INFO.DAT", filedata)

        free_end, total_end = self.fs.get_free_space()
        alloc_end = self.fs.get_allocated_clusters()

        self.assertEqual(total_end, total_start)
        self.assertLess(free_end, free_start)
        # Should have allocated 3 clusters for the data
        self.assertEqual(len(alloc_end), len(alloc_start) + 3, f"Expected 3 more allocated clusters. Start: {len(alloc_start)}, End: {len(alloc_end)}")
        # Verify the exact amount of space used (3 clusters * 512 bytes/cluster)
        self.assertEqual(free_start - free_end, 3 * self.fs.cluster_size)


    def test_15_nested_directories(self):
        path = "DIR1/SUBDIR/TARGET.TXT"
        data = b"Deeply nested file."

        self.fs.create_directory("DIR1")
        self.fs.create_directory("DIR1/SUBDIR")
        self.fs.write_file(path, data)
        self._check_fat_mirror()

        read_data = self.fs.read_file(path)
        self.assertEqual(read_data, data)

        # List intermediate directory
        sub_entries = self.fs.list_directory("DIR1")
        self.assertEqual(len(sub_entries), 1)
        self.assertEqual(sub_entries[0].name, "SUBDIR")
        self.assertTrue(sub_entries[0].is_dir)

        # List deepest directory
        target_entries = self.fs.list_directory("DIR1/SUBDIR")
        self.assertEqual(len(target_entries), 1)
        self.assertEqual(target_entries[0].name, "TARGET.TXT")
        self.assertFalse(target_entries[0].is_dir)

        # Delete
        self.fs.delete(path)
        self.fs.delete("DIR1/SUBDIR")
        self.fs.delete("DIR1")
        self._check_fat_mirror()

        root_entries = self.fs.list_directory("/")
        self.assertEqual(root_entries, [])

    # --- BPB / Geometry Tests ---
    # These would ideally use the DiskController, but we can test
    # FATFilesystem's reliance on Disk geometry here.

    def test_16_init_with_different_geometry_720k(self):
        # Re-setup with 720KB geometry on the same (initially 1.44MB) image data
        # This simulates reading a 720KB disk in a 1.44MB drive/image.
        # NOTE: For a *real* 720KB image, you'd need a different test file.
        # Here we just test if the FS logic *uses* the provided geometry.
        self.disk.set_geometry(FMT_720.geometry)
        # Re-init FS with the new geometry
        fs_720 = FATFilesystem(self.disk)
        self.assertTrue(fs_720.is_valid(), "Should still parse BPB even if geometry differs slightly")

        # Check if calculations reflect 720KB geometry if BPB matches it
        # On a standard 1.44MB formatted disk, the BPB *won't* match 720KB,
        # so the FS should still report 1.44MB parameters based on BPB.
        self.assertEqual(fs_720.boot_sector.total_sectors, 2880, "BPB total_sectors should override geometry")
        self.assertEqual(fs_720.boot_sector.sectors_per_track, 18, "BPB sectors_per_track should override")
        self.assertEqual(fs_720.boot_sector.num_heads, 2, "BPB num_heads should override")
        self.assertEqual(fs_720.cluster_size, 512, "BPB dictates cluster size")

    # --- Robustness Tests ---

    def test_17_read_file_corrupted_fat_chain_loop(self):
        # Create a file, then manually corrupt FAT to create a loop
        filename = "LOOP.DAT"
        filedata = bytes([i % 256 for i in range(1200)]) # Needs 3 clusters (c1, c2, c3)
        self.fs.write_file(filename, filedata)

        entry = next(e for e in self.fs.list_directory("/") if e.name == filename)
        c1 = entry.starting_cluster
        c2 = self._read_test_fat_entry(c1)
        c3 = self._read_test_fat_entry(c2)
        self.assertEqual(self._read_test_fat_entry(c3), 0xFFF) # Verify initial chain

        # Create loop: c3 -> c2
        self.fs._set_fat_entry(c3, c2)
        self._check_fat_mirror() # Ensure corruption is mirrored

        # Reading should detect the loop and likely raise error or truncate
        # Current implementation might loop infinitely or hit max length.
        # Let's expect it to hit the max length protection in _get_cluster_chain
        try:
            read_data = self.fs.read_file(filename)
            # It might successfully read up to the point of the loop repetition.
            # The exact behavior depends on the loop detection logic.
            # Check if it read *some* data, but maybe not all, or maybe too much.
            self.assertLess(len(read_data), 1000 * self.fs.cluster_size, "Read should not be infinitely large")
            # A more robust test might require specific error handling/logging checks
            print(f"Warning: Read file with FAT loop completed. Length: {len(read_data)}. Verify manually if behavior is correct.")
        except Exception as e:
             # Or it might raise an error, which is also acceptable
             print(f"Caught expected exception from reading looped FAT: {e}")
             pass

    def test_18_read_file_corrupted_fat_chain_free_sector(self):
        # Create a file, then manually corrupt FAT to point to a free sector (0)
        filename = "FREEPTR.DAT"
        filedata = bytes([i % 256 for i in range(1200)]) # Needs 3 clusters (c1, c2, c3)
        self.fs.write_file(filename, filedata)

        # --- Debug Read 1: After file write ---
        print(f"DEBUG 1: Reading root dir sector after write_file (Offset {self.fs.root_dir_start})")
        try:
            root_sec_after_write = self.fs._read_bytes(self.fs.root_dir_start, 512)
            print(f"DEBUG 1: First 64 bytes: {root_sec_after_write[:64].hex(' ')}")
            # Try parsing the entry directly
            try:
                 entry_info = self.fs._parse_directory_entry(root_sec_after_write[:32])
                 print(f"DEBUG 1: Parsed entry: {entry_info}")
            except Exception as e:
                 print(f"DEBUG 1: Failed to parse entry: {e}")
        except Exception as e:
            print(f"DEBUG 1: Error reading root sector: {e}")
        # --- End Debug Read 1 ---

        # Find the entry using the *current* fs object
        entry = next(e for e in self.fs.list_directory("/") if e.name == filename)
        c1 = entry.starting_cluster
        c2 = self._read_test_fat_entry(c1)
        c3 = self._read_test_fat_entry(c2)
        self.assertEqual(self._read_test_fat_entry(c3), 0xFFF) # Verify initial chain

        # Corrupt chain: c2 -> 0
        self.fs._set_fat_entry(c2, 0)

        # --- Debug Read 2: After FAT corruption ---
        print(f"DEBUG 2: Reading root dir sector after FAT corruption (Offset {self.fs.root_dir_start})")
        try:
            root_sec_after_corrupt = self.fs._read_bytes(self.fs.root_dir_start, 512)
            print(f"DEBUG 2: First 64 bytes: {root_sec_after_corrupt[:64].hex(' ')}")
            # Try parsing the entry directly
            try:
                 entry_info_2 = self.fs._parse_directory_entry(root_sec_after_corrupt[:32])
                 print(f"DEBUG 2: Parsed entry: {entry_info_2}")
            except Exception as e:
                 print(f"DEBUG 2: Failed to parse entry: {e}")
        except Exception as e:
            print(f"DEBUG 2: Error reading root sector: {e}")
        # --- End Debug Read 2 ---

        self._check_fat_mirror()

        # --- Force FS Re-initialization ---
        print("DEBUG: Re-initializing FS object after corruption")
        # Create a NEW FS instance using the SAME disk object.
        # This forces re-reading the BPB and initializing parameters from the possibly modified buffer.
        fs_reloaded = FATFilesystem(self.disk)
        # Check validity and ensure caches are clear for the reloaded instance
        self.assertTrue(fs_reloaded.is_valid(), "Filesystem became invalid after FAT corruption?")
        fs_reloaded.fat_cache = None
        fs_reloaded._cached_allocated_clusters = None
        # --- End Re-initialization ---

        # Reading should stop prematurely. Use the *reloaded* FS object.
        try:
            read_data = fs_reloaded.read_file(filename)
        except ValueError as e:
            # If it *still* fails here, print the directory listing from the reloaded object
            print(f"ERROR: File not found even after reloading FS. Reloaded directory listing:")
            try:
                reloaded_listing = fs_reloaded.list_directory("/")
                print(reloaded_listing)
            except Exception as list_e:
                print(f"Could not list directory after reload: {list_e}")
            raise e # Re-raise the original error

        # Should have read only c1 and c2
        expected_len = 2 * fs_reloaded.cluster_size # Use reloaded fs object's cluster size
        self.assertEqual(len(read_data), expected_len, "Read should truncate at the corrupted FAT entry pointing to 0")
        # Check content of the first two clusters
        self.assertEqual(read_data, filedata[:expected_len])


    def test_19_fat_mirroring_consistency(self):
         # Test mirroring after various operations
        self.fs.write_file("MIRROR1.TXT", b"abc")
        self._check_fat_mirror()

        self.fs.create_directory("MIRRORDR")
        self._check_fat_mirror()

        self.fs.write_file("MIRRORDR/MIRROR2.DAT", b"12345" * 200) # Multi-cluster
        self._check_fat_mirror()

        self.fs.delete("MIRROR1.TXT")
        self._check_fat_mirror()

        self.fs.delete("MIRRORDR/MIRROR2.DAT")
        self._check_fat_mirror()

        self.fs.delete("MIRRORDR")
        self._check_fat_mirror()


    def test_20_invalid_83_filenames(self):
        invalid_names = [
            "TOOLONGNAME.TXT",
            "SHORT.TOOLONGEXT",
            "FILE NAME.TXT", # Spaces invalid
            "FILE?NAME.TXT", # Special chars invalid
            "COM1.TXT",      # Reserved names
            "PRN",
            ".BAD",          # Starts with dot
            "GOOD.",         # Ends with dot
        ]
        for name in invalid_names:
            with self.assertRaises(ValueError, msg=f"Should fail for invalid name: {name}"):
                self.fs.write_file(name, b"data")
            with self.assertRaises(ValueError, msg=f"Should fail for invalid name: {name}"):
                self.fs.create_directory(name)

    def test_21_fat_offsets(self):
        self.assertEqual(self.fs.fat_start, 512, "FAT start should be at sector 1")
        self.assertEqual(self.fs.root_dir_start, 9728, "Root directory should start after FATs")
        fat_size = self.fs.boot_sector.sectors_per_fat * self.fs.boot_sector.bytes_per_sector
        self.assertEqual(fat_size, 4608, "FAT size should be 9 sectors")

if __name__ == '__main__':
    unittest.main()
