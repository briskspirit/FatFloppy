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
# Import specific errors if needed for asserts
from fatfloppy.core.filesystem import FATFilesystem, FileInfo, ATTR_VOLUME_ID, ATTR_LONG_NAME
from fatfloppy.core.format_definitions import FLOPPY_FORMATS

RESOURCE_DIR = os.path.join(os.path.dirname(__file__), 'resources')
EMPTY_IMG = os.path.join(RESOURCE_DIR, 'empty_1.44mb.img')
POPULATED_IMG = os.path.join(RESOURCE_DIR, 'populated_1.44mb.img')
TEST_FILE_TXT = os.path.join(RESOURCE_DIR, 'test_file.txt') # Corrected filename
PATTERN_FILE_BIN = os.path.join(RESOURCE_DIR, 'pattern_file.bin') # Corrected filename

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
        self.driver.set_physical_format(FMT_144.physical_format) # Ensure driver knows format

        self.fs = FATFilesystem(self.disk)
        # Clear caches to ensure tests start fresh
        self.fs.fat_cache = None
        self.fs._cached_allocated_clusters = None
        # Force load cache if needed (will be loaded on first access anyway by logic)
        # self.fs._load_fat_cache()

        print(f"\nRunning test: {self.id()}")
        self.assertTrue(self.fs.is_valid(), "Filesystem should be valid on setup")


    def tearDown(self):
        print(f"Starting tearDown for: {self.id()}")
        # --- Force Resource Release and Deletion Order ---

        fs_to_del = getattr(self, 'fs', None)
        disk_to_del = getattr(self, 'disk', None)
        driver_to_del = getattr(self, 'driver', None)
        temp_dir_to_del = getattr(self, 'temp_dir', None) # Use separate var

        if hasattr(self, 'fs'): self.fs = None
        if hasattr(self, 'disk'): self.disk = None
        if hasattr(self, 'driver'): self.driver = None
        if hasattr(self, 'temp_dir'): self.temp_dir = None

        del fs_to_del
        del disk_to_del
        del driver_to_del

        if temp_dir_to_del and os.path.exists(temp_dir_to_del):
            print(f"Attempting to remove directory: {temp_dir_to_del}")
            try:
                shutil.rmtree(temp_dir_to_del)
                print(f"Successfully removed directory: {temp_dir_to_del}")
            except OSError as e:
                print(f"ERROR: Failed to remove temp dir '{temp_dir_to_del}' in tearDown: {e}")
        elif temp_dir_to_del:
             print(f"Directory not found for removal: {temp_dir_to_del}")

        print(f"Finished tearDown for: {self.id()}")

    # --- Helper to read FAT entry (simplistic for testing) ---
    def _read_test_fat_entry(self, cluster: int):
         if not self.fs.is_valid(): return None
         # FIX: Use fat_start_offset
         fat_offset = self.fs.fat_start_offset + int(cluster * 1.5)
         try:
             value_bytes = self.fs._read_bytes(fat_offset, 2)
             if len(value_bytes) < 2: return None # Handle short read
             value = struct.unpack('<H', value_bytes)[0]
         except (IOError, ValueError, struct.error) as e:
             print(f"Warning: Error reading test FAT entry for cluster {cluster}: {e}")
             return None # Return None on read error

         if cluster % 2 == 0:
             return value & 0x0FFF
         else:
             return value >> 4

    # --- Helper to check FAT mirroring ---
    def _check_fat_mirror(self):
        if not self.fs.is_valid() or self.fs.boot_sector.num_fats < 2:
            print("DEBUG: Skipping FAT mirror check (FS invalid or <2 FATs)")
            return True # No second FAT to check

        # FIX: Use fat_start_offset
        fat1_offset = self.fs.fat_start_offset
        fat_size = self.fs.boot_sector.sectors_per_fat * self.fs.boot_sector.bytes_per_sector
        fat2_offset = fat1_offset + fat_size

        try:
            fat1_data = self.fs._read_bytes(fat1_offset, fat_size)
            fat2_data = self.fs._read_bytes(fat2_offset, fat_size)
            self.assertEqual(fat1_data, fat2_data, f"FAT tables (size {fat_size}) are not mirrored correctly. FAT1 starts @ {fat1_offset}, FAT2 starts @ {fat2_offset}")
            print(f"DEBUG: FAT mirror check PASSED (offset {fat1_offset} vs {fat2_offset}, size {fat_size})")
        except Exception as e:
            self.fail(f"Error during FAT mirror check: {e}")


    def test_01_initialization_valid(self):
        self.assertTrue(self.fs.is_valid())
        self.assertEqual(self.fs.fat_type, "FAT12")
        self.assertGreater(self.fs.cluster_size, 0)
        self.assertGreater(self.fs.num_clusters, 0)
        # Check specific 1.44MB params
        self.assertEqual(self.fs.boot_sector.total_sectors, 2880)
        self.assertEqual(self.fs.cluster_size, 512) # 1 sector/cluster for 1.44
        # Recalculate expected clusters based on initialized offsets
        # data_area_start_sector = (self.fs.data_area_start_offset + self.fs.boot_sector.bytes_per_sector -1) // self.fs.boot_sector.bytes_per_sector
        # total_data_sectors = self.fs.boot_sector.total_sectors - data_area_start_sector
        # expected_clusters = total_data_sectors // self.fs.boot_sector.sectors_per_cluster
        # self.assertEqual(self.fs.num_clusters, expected_clusters) # Should match internal calc
        self.assertEqual(self.fs.num_clusters, 2847) # Hardcoded known value for 1.44MB


    def test_02_initialization_invalid_boot_sig(self):
        # Corrupt boot signature in the image data held by the *driver*
        # Ensure the driver's data is modified *before* FS init
        driver_corrupt = RawImageDriver(self.test_img_path) # Read fresh data
        driver_corrupt.image_data[510:512] = b'\x00\x00' # Corrupt it
        disk_corrupt = Disk(driver_corrupt)
        disk_corrupt.set_geometry(FMT_144.geometry) # Set geometry

        fs_corrupt = FATFilesystem(disk_corrupt) # Initialize FS with corrupted data
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
        self.assertIsNotNone(entry.starting_cluster, "Starting cluster should not be None")
        self.assertGreater(entry.starting_cluster, 1) # Should be allocated

        # Verify FAT entry for the first cluster
        fat_val = self._read_test_fat_entry(entry.starting_cluster)
        self.assertEqual(fat_val, 0xFFF, f"Single cluster file should end with 0xFFF, got {fat_val} for cluster {entry.starting_cluster}")

        # Verify free space decreased
        # Force recalculation by clearing cache before first read
        self.fs._cached_allocated_clusters = None
        free_before, total = self.fs.get_free_space()
        self.fs._cached_allocated_clusters = None # Clear again before write
        self.fs.write_file("DUMMY.DAT", b"data") # Write something small
        self.fs._cached_allocated_clusters = None # Clear before read after write
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
        self.assertIsNotNone(start_cluster, "Entry cluster should not be None")
        self.assertGreater(start_cluster, 1)

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
        # Force recalculation
        self.fs._cached_allocated_clusters = None
        free_before, total = self.fs.get_free_space()
        self.fs._cached_allocated_clusters = None
        self.fs.write_file("DUMMY2.DAT", b"data") # Write
        self.fs._cached_allocated_clusters = None
        self.fs.delete("DUMMY2.DAT") # Delete
        self.fs._cached_allocated_clusters = None
        free_after, _ = self.fs.get_free_space() # Recalc
        # Allow for possibility that DUMMY2 didn't change free clusters if root was full etc.
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
        self.assertIsNotNone(entry.starting_cluster)
        self.assertGreater(entry.starting_cluster, 1)

        # Verify directory cluster contains . and ..
        # Use internal method to bypass filtering
        dir_entries = self.fs._list_directory_by_cluster(entry.starting_cluster)
        # Filter out potential volume labels or other non-file/dir entries if parser returns them
        filtered_dir_entries = [e for e in dir_entries if not (e.attributes and ("VOL" in e.attributes or "LFN" in e.attributes))]

        self.assertEqual(len(filtered_dir_entries), 2, f"Expected 2 entries (. ..) found {len(filtered_dir_entries)}: {filtered_dir_entries}")
        self.assertEqual(filtered_dir_entries[0].name, ".")
        self.assertEqual(filtered_dir_entries[0].starting_cluster, entry.starting_cluster)
        self.assertEqual(filtered_dir_entries[1].name, "..")
        self.assertEqual(filtered_dir_entries[1].starting_cluster, 0) # Parent is root

        # Verify FAT entry for the directory cluster
        fat_val = self._read_test_fat_entry(entry.starting_cluster)
        self.assertEqual(fat_val, 0xFFF, "Single cluster directory should end with 0xFFF")

    def test_08_create_file_in_subdir(self):
        dirname = "SUB"
        filename = "INSIDE.DAT"
        filepath = f"{dirname}/{filename}" # Test with relative path
        filedata = b"Data inside a subdirectory."

        self.fs.create_directory(dirname)
        self.fs.write_file(filepath, filedata)
        self._check_fat_mirror()

        # Check listing the subdirectory
        sub_entries = self.fs.list_directory(dirname) # Use relative dir name
        self.assertEqual(len(sub_entries), 1)
        entry = sub_entries[0]
        self.assertEqual(entry.name, filename)
        self.assertEqual(entry.size, len(filedata))
        self.assertFalse(entry.is_dir)

        # Read back the file using relative path
        read_data = self.fs.read_file(filepath)
        self.assertEqual(read_data, filedata)

    def test_09_delete_empty_directory(self):
        dirname = "EMPTYDIR"
        self.fs.create_directory(dirname)
        dir_entry = next(e for e in self.fs.list_directory("/") if e.name == dirname)
        start_cluster = dir_entry.starting_cluster
        self.assertIsNotNone(start_cluster)
        self.assertGreater(start_cluster, 1)


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

        # FIX: Expect OSError, not ValueError
        with self.assertRaisesRegex(OSError, "Directory not empty"):
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
        self.assertIsNotNone(c1)
        self.assertGreater(c1, 1)

        c2 = self._read_test_fat_entry(c1)
        self.assertIsNotNone(c2, f"FAT entry for cluster {c1} is None")
        self.assertGreater(c2, 1, f"FAT entry for cluster {c1} should point to valid cluster > 1, got {c2}")
        self.assertNotEqual(c1, c2)

        c3 = self._read_test_fat_entry(c2)
        self.assertIsNotNone(c3, f"FAT entry for cluster {c2} is None")
        self.assertGreater(c3, 1, f"FAT entry for cluster {c2} should point to valid cluster > 1, got {c3}")
        self.assertNotEqual(c2, c3)
        self.assertNotEqual(c1, c3)

        end_marker = self._read_test_fat_entry(c3)
        self.assertIsNotNone(end_marker, f"FAT entry for cluster {c3} is None")
        self.assertEqual(end_marker, 0xFFF, f"Third cluster {c3} should be end of chain (FFF), got {end_marker}")

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
        self.assertIsNotNone(cluster1)

        self.fs.write_file(filename, new_data)
        self._check_fat_mirror()

        entry2 = next(e for e in self.fs.list_directory("/") if e.name == filename)
        cluster2 = entry2.starting_cluster
        size2 = entry2.size
        self.assertIsNotNone(cluster2)


        self.assertEqual(size2, len(new_data))
        self.assertNotEqual(size1, size2)
        # Cluster might or might not change, don't assert equality/inequality
        # self.assertNotEqual(cluster1, cluster2) # This is not guaranteed

        read_data = self.fs.read_file(filename)
        self.assertEqual(read_data, new_data)

    def test_14_filesystem_info(self):
        # Clear caches before getting initial state
        self.fs._cached_allocated_clusters = None
        free_start, total_start = self.fs.get_free_space()
        alloc_start = self.fs.get_allocated_clusters()

        # FIX: Compare total_start to calculated data area size
        expected_data_bytes = self.fs.num_clusters * self.fs.cluster_size
        self.assertEqual(total_start, expected_data_bytes, "get_free_space total should be data area size")
        self.assertGreater(free_start, 0)
        self.assertEqual(len(alloc_start), 0, "Expected 0 allocated clusters initially")

        # Write a file using 3 clusters
        filedata = bytes([i % 256 for i in range(1200)]) # Needs 3 clusters
        self.fs._cached_allocated_clusters = None # Clear cache before write
        self.fs.write_file("INFO.DAT", filedata)

        # Clear caches before getting final state
        self.fs._cached_allocated_clusters = None
        free_end, total_end = self.fs.get_free_space()
        alloc_end = self.fs.get_allocated_clusters()

        self.assertEqual(total_end, total_start)
        self.assertLess(free_end, free_start)
        self.assertEqual(len(alloc_end), 3, f"Expected 3 allocated clusters. Found: {len(alloc_end)}")
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

    # --- Robustness Tests ---

    def test_16_read_file_corrupted_fat_chain_loop(self):
        filename = "LOOP.DAT"
        filedata = bytes([i % 256 for i in range(1200)]) # Needs 3 clusters
        self.fs.write_file(filename, filedata)

        entry = next(e for e in self.fs.list_directory("/") if e.name == filename)
        c1 = entry.starting_cluster
        c2 = self._read_test_fat_entry(c1)
        c3 = self._read_test_fat_entry(c2)
        self.assertIsNotNone(c1); self.assertIsNotNone(c2); self.assertIsNotNone(c3)
        self.assertEqual(self._read_test_fat_entry(c3), 0xFFF)

        # Create loop: c3 -> c2
        # Use the internal _set_fat_entry_cached and _commit_fat for corruption
        self.fs._set_fat_entry_cached(c3, c2)
        self.fs._commit_fat() # Write corrupted FAT
        self._check_fat_mirror()

        # Clear caches before reading
        self.fs.fat_cache = None
        fs_reloaded = FATFilesystem(self.disk) # Re-init to reload FAT
        self.assertTrue(fs_reloaded.is_valid())

        try:
            read_data = fs_reloaded.read_file(filename)
            # Behavior depends on implementation: might truncate or error.
            # Check if length is plausible (not infinite, not zero unless expected).
            self.assertLess(len(read_data), fs_reloaded.num_clusters * fs_reloaded.cluster_size, "Read size excessive, possible loop issue")
            print(f"Warning: Read file with FAT loop completed. Length: {len(read_data)}. Verify manually if behavior is correct.")
            # Optional: Check if it read at least the first few clusters correctly
            self.assertGreaterEqual(len(read_data), 2 * fs_reloaded.cluster_size)
        except (IOError, ValueError, IndexError) as e:
             print(f"Caught expected exception from reading looped FAT: {e}")
             pass # Catching an error here is also acceptable

    def test_17_read_file_corrupted_fat_chain_free_sector(self):
        filename = "FREEPTR.DAT"
        filedata = bytes([i % 256 for i in range(1200)]) # Needs 3 clusters
        self.fs.write_file(filename, filedata)

        # Debug log before corruption
        # print(f"DEBUG 1: Before corruption. Root dir offset {self.fs.root_dir_start_offset}") # FIX: Name
        # root_data_pre = self.fs._read_bytes(self.fs.root_dir_start_offset, 64) # FIX: Name
        # print(f"DEBUG 1: Root(pre): {root_data_pre.hex(' ')}")

        entry = next(e for e in self.fs.list_directory("/") if e.name == filename)
        c1 = entry.starting_cluster
        c2 = self._read_test_fat_entry(c1)
        c3 = self._read_test_fat_entry(c2)
        self.assertIsNotNone(c1); self.assertIsNotNone(c2); self.assertIsNotNone(c3)
        self.assertEqual(self._read_test_fat_entry(c3), 0xFFF)

        # Corrupt chain: c2 -> 0
        self.fs._set_fat_entry_cached(c2, 0) # Use cache method
        self.fs._commit_fat() # Write corrupted FAT
        self._check_fat_mirror()

        # Debug log after corruption
        # print(f"DEBUG 2: After corruption.")
        # root_data_post = self.fs._read_bytes(self.fs.root_dir_start_offset, 64) # FIX: Name
        # print(f"DEBUG 2: Root(post): {root_data_post.hex(' ')}")
        # print(f"DEBUG 2: FAT entry for c2 ({c2}): {self._read_test_fat_entry(c2)}")

        # --- Force FS Re-initialization ---
        print("DEBUG: Re-initializing FS object after corruption")
        self.fs.fat_cache = None # Clear cache of old object first
        fs_reloaded = FATFilesystem(self.disk)
        self.assertTrue(fs_reloaded.is_valid(), "Filesystem became invalid after FAT corruption?")
        fs_reloaded.fat_cache = None # Ensure reloaded cache is clear too
        fs_reloaded._cached_allocated_clusters = None

        # Reading should stop prematurely. Use the *reloaded* FS object.
        try:
            read_data = fs_reloaded.read_file(filename)
            # Should have read only c1
            expected_len = 2 * fs_reloaded.cluster_size
            self.assertEqual(len(read_data), expected_len, f"Read should truncate at the corrupted FAT entry pointing to 0. Got {len(read_data)}, expected {expected_len}")
            self.assertEqual(read_data, filedata[:expected_len])
        except FileNotFoundError:
            # Check if the entry itself vanished somehow
            print("ERROR: FileNotFoundError when reading corrupted chain. Listing root:")
            print(fs_reloaded.list_directory("/"))
            self.fail("FileNotFoundError encountered unexpectedly.")
        except Exception as e:
             print(f"ERROR: Unexpected exception reading corrupted chain: {e}")
             self.fail(f"Unexpected exception: {e}")


    def test_18_fat_mirroring_consistency(self):
         self.fs.write_file("MIRROR1.TXT", b"abc")
         self._check_fat_mirror()
         self.fs.create_directory("MIRRORDR")
         self._check_fat_mirror()
         self.fs.write_file("MIRRORDR/MIRROR2.DAT", b"12345" * 200)
         self._check_fat_mirror()
         self.fs.delete("MIRROR1.TXT")
         self._check_fat_mirror()
         self.fs.delete("MIRRORDR/MIRROR2.DAT")
         self._check_fat_mirror()
         self.fs.delete("MIRRORDR")
         self._check_fat_mirror()

    def test_19_write_zero_byte_file(self):
        filename = "ZERO.DAT"
        self.fs.write_file(filename, b"")
        self._check_fat_mirror()

        entries = self.fs.list_directory("/")
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.name, filename)
        self.assertEqual(entry.size, 0)
        self.assertFalse(entry.is_dir)
        # FIX: Zero byte files might have starting_cluster=0
        # self.assertGreater(entry.starting_cluster, 1, "Starting cluster should be allocated (>1)")
        self.assertIsNotNone(entry.starting_cluster, "Starting cluster should not be None")
        # Check if a cluster was allocated (depends on implementation detail)
        # Let's check the FAT entry for cluster 0, which should remain 0
        # A cluster might be allocated but the chain immediately terminated.
        start_cluster_for_zero = entry.starting_cluster
        if start_cluster_for_zero == 0:
             print("DEBUG: Zero-byte file created with cluster 0 (correct).")
        elif start_cluster_for_zero >= 2:
             print(f"DEBUG: Zero-byte file created with cluster {start_cluster_for_zero}.")
             fat_val = self._read_test_fat_entry(start_cluster_for_zero)
             # A valid implementation could allocate a cluster and mark it EOF
             self.assertEqual(fat_val, 0xFFF, f"Allocated cluster {start_cluster_for_zero} for zero-byte file should be marked EOF (FFF)")
        else:
             self.fail(f"Invalid starting cluster {start_cluster_for_zero} for zero-byte file.")


        # Read back
        read_data = self.fs.read_file(filename)
        self.assertEqual(read_data, b"")

        # Delete
        self.fs.delete(filename)
        self._check_fat_mirror()
        self.assertEqual(self.fs.list_directory("/"), [])

        # Verify cluster (if allocated) is freed
        if start_cluster_for_zero >= 2:
            fat_val_after = self._read_test_fat_entry(start_cluster_for_zero)
            self.assertEqual(fat_val_after, 0, "Cluster should be freed after deleting zero-byte file")

    def test_20_read_zero_byte_file(self):
        filename = "ZEROBYTE.FIL"
        now = datetime.datetime.now()
        # FIX: Use _create_directory_entry_bytes
        entry_data = self.fs._create_directory_entry_bytes(filename, False, 0, 0, now)
        # FIX: Use root_dir_start_offset
        entry_offset = self.fs.root_dir_start_offset
        self.fs._write_bytes(entry_offset, entry_data)

        read_data = self.fs.read_file(filename)
        self.assertEqual(read_data, b"")

    def test_21_init_with_different_geometry_720k(self):
        self.disk.set_geometry(FMT_720.geometry)
        # Re-init FS - it should read BPB from disk, ignoring the new geometry set *after* initial FS load
        fs_reinit = FATFilesystem(self.disk)
        self.assertTrue(fs_reinit.is_valid())

        # Verify it uses the BPB values from the underlying 1.44MB formatted image data
        self.assertEqual(fs_reinit.boot_sector.total_sectors, 2880)
        self.assertEqual(fs_reinit.boot_sector.sectors_per_track, 18)
        self.assertEqual(fs_reinit.boot_sector.num_heads, 2)
        self.assertEqual(fs_reinit.cluster_size, 512)


    def test_22_invalid_83_filenames(self):
        invalid_names = [
            "TOOLONGNAME.TXT",
            "SHORT.TOOLONGEXT",
            "FILE NAME.TXT",
            "FILE?NAME.TXT",
            "COM1.TXT",
            "PRN",
            ".BAD",
            # "GOOD.", # This might be allowed depending on strictness, let's test separately
        ]
        for name in invalid_names:
            with self.assertRaises(ValueError, msg=f"Should fail for invalid name: {name}"):
                self.fs.write_file(name, b"data")
            # Directory names have stricter rules (no extension)
            if '.' not in name and ' ' not in name and '?' not in name and '*' not in name \
               and name.upper() not in ["COM1", "PRN"] and not name.startswith('.'):
                 with self.assertRaises(ValueError, msg=f"Should fail for invalid dir name: {name}"):
                     self.fs.create_directory(name)

        # Test trailing dot separately - should fail now
        with self.assertRaisesRegex(ValueError, "Invalid 8.3 filename", msg="Should fail for name ending in dot: GOOD."):
             self.fs.write_file("GOOD.", b"data")
        with self.assertRaisesRegex(ValueError, "Invalid 8.3 directory name", msg="Should fail for dir name ending in dot: GOODDIR."):
             self.fs.create_directory("GOODDIR.")


    def test_23_fat_offsets(self):
        # FIX: Use fat_start_offset and root_dir_start_offset
        self.assertEqual(self.fs.fat_start_offset, 512, "FAT start should be at sector 1 (offset 512)")
        fat_size_bytes = self.fs.boot_sector.sectors_per_fat * self.fs.boot_sector.bytes_per_sector
        expected_root_start = self.fs.fat_start_offset + (self.fs.boot_sector.num_fats * fat_size_bytes)
        self.assertEqual(self.fs.root_dir_start_offset, expected_root_start, "Root directory should start after FATs")
        # Original test had 9728, let's verify: 512 + 2 * (9 * 512) = 512 + 2 * 4608 = 512 + 9216 = 9728. Correct.
        self.assertEqual(self.fs.root_dir_start_offset, 9728)
        self.assertEqual(fat_size_bytes, 4608, "FAT size should be 9 sectors * 512 bytes/sector")

if __name__ == '__main__':
    unittest.main()
