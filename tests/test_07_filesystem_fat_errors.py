# tests/test_07_filesystem_fat_errors.py
import unittest
from unittest.mock import MagicMock, patch, PropertyMock, call # Import call
import os
import sys
import datetime
import struct

# Ensure src is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from fatfloppy.core.filesystem import FATFilesystem, FATBootSector, FileInfo
from fatfloppy.core.disk import Disk, DiskGeometry
from fatfloppy.core.drivers import DiskIODriver # Use generic driver mock


# Minimal valid BPB for FAT12 (e.g., 720k-like) - Reused from test setup
SECTOR_SIZE = 512
SECTORS_PER_CLUSTER = 2
RESERVED_SECTORS = 1
NUM_FATS = 2
ROOT_ENTRIES = 112
TOTAL_SECTORS = 1440 # 720KB
SECTORS_PER_FAT = 3
SECTORS_PER_TRACK = 9
NUM_HEADS = 2
CLUSTER_SIZE = SECTOR_SIZE * SECTORS_PER_CLUSTER
ROOT_DIR_SECTORS = (ROOT_ENTRIES * 32 + SECTOR_SIZE - 1) // SECTOR_SIZE
FAT_SIZE_BYTES = SECTORS_PER_FAT * SECTOR_SIZE
FAT_START_SECTOR = RESERVED_SECTORS
ROOT_DIR_START_SECTOR = FAT_START_SECTOR + NUM_FATS * SECTORS_PER_FAT
DATA_AREA_START_SECTOR = ROOT_DIR_START_SECTOR + ROOT_DIR_SECTORS
NUM_CLUSTERS = (TOTAL_SECTORS - DATA_AREA_START_SECTOR) // SECTORS_PER_CLUSTER

ATTR_READ_ONLY = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_VOLUME_ID = 0x08
ATTR_LONG_NAME = ATTR_READ_ONLY | ATTR_HIDDEN | ATTR_SYSTEM | ATTR_VOLUME_ID

class TestFATFilesystemErrors(unittest.TestCase):

    def setUp(self):
        print(f"\n--- Running test: {self.id()} ---") # Add separator for clarity
        self.mock_driver = MagicMock(spec=DiskIODriver)
        self.mock_disk = MagicMock(spec=Disk)
        self.mock_disk.driver = self.mock_driver
        self.mock_disk_geometry = DiskGeometry(
            cylinders=TOTAL_SECTORS // (SECTORS_PER_TRACK * NUM_HEADS),
            heads=NUM_HEADS,
            sectors_per_track=SECTORS_PER_TRACK,
            sector_size=SECTOR_SIZE
        )
        # Reset mocks for geometry property in each test setup
        # Use a new PropertyMock instance each time
        type(self.mock_disk).geometry = PropertyMock(return_value=self.mock_disk_geometry)

        self.mock_boot_sector_data = bytearray(SECTOR_SIZE)
        struct.pack_into('<H', self.mock_boot_sector_data, 0x0B, SECTOR_SIZE)
        self.mock_boot_sector_data[0x0D] = SECTORS_PER_CLUSTER
        struct.pack_into('<H', self.mock_boot_sector_data, 0x0E, RESERVED_SECTORS)
        self.mock_boot_sector_data[0x10] = NUM_FATS
        struct.pack_into('<H', self.mock_boot_sector_data, 0x11, ROOT_ENTRIES)
        # Use TOTAL_SECTORS consistently
        struct.pack_into('<H', self.mock_boot_sector_data, 0x13, TOTAL_SECTORS) # Small disk <= 32MB uses 16-bit field
        struct.pack_into('<I', self.mock_boot_sector_data, 0x20, 0) # 32-bit field is 0
        self.mock_boot_sector_data[0x15] = 0xF9 # Media desc for 720k floppy often F9
        struct.pack_into('<H', self.mock_boot_sector_data, 0x16, SECTORS_PER_FAT)
        struct.pack_into('<H', self.mock_boot_sector_data, 0x18, SECTORS_PER_TRACK)
        struct.pack_into('<H', self.mock_boot_sector_data, 0x1A, NUM_HEADS)
        struct.pack_into('<I', self.mock_boot_sector_data, 0x1C, 0) # Hidden
        struct.pack_into('<H', self.mock_boot_sector_data, 0x1FE, 0xAA55) # Signature

        def mock_read_sector(c, h, s):
             if c == 0 and h == 0 and s == 1:
                 # print("DEBUG setUp Mock read_sector: Returning boot sector")
                 return bytes(self.mock_boot_sector_data)
             # print(f"DEBUG setUp Mock read_sector: Returning zeros for C:{c} H:{h} S:{s}")
             return bytes(SECTOR_SIZE)
        # Make sure read_sector is attached to the mock_disk instance
        self.mock_disk.read_sector = MagicMock(side_effect=mock_read_sector)

        # Default mock for read_bytes used by FAT init etc.
        self.mock_disk._read_bytes = MagicMock(return_value=b'\x00' * 4096)

        # Create a fresh FS instance for each test
        self.fs = FATFilesystem(self.mock_disk)

        # Check validity immediately after creation
        # This depends on _read_bytes being mocked to return *something* for the FAT read during init
        # If init fails here, the test setup is broken.
        if not self.fs.is_valid():
             print("FS PARAMS:")
             print(f"  BPB Valid: {self.fs.boot_sector.is_valid() if self.fs.boot_sector else 'No BPB'}")
             if self.fs.boot_sector and self.fs.boot_sector.is_valid():
                 print(f"  FS Type: {self.fs.fat_type}")
                 print(f"  FAT Offset: {self.fs.fat_start_offset}")
                 print(f"  FAT Size: {self.fs.fat_size_bytes}")
             print(f"  Mock Disk Reads: {self.mock_disk._read_bytes.call_args_list}")

        self.assertTrue(self.fs.is_valid(), "Filesystem initialization failed in setUp")


        # --- Setup for FAT operations mocking (consistent with FS init) ---
        self.fat_data = bytearray(FAT_SIZE_BYTES) # Size of ONE FAT copy
        self.fat_data[0:3] = bytes([0xF9, 0xFF, 0xFF]) # FAT ID bytes

        def read_bytes_side_effect(offset, length):
            # Use the calculated offsets from the *initialized* fs object
            fat_start_offset = self.fs.fat_start_offset
            # Check if read is within the first FAT copy
            if offset >= fat_start_offset and offset < fat_start_offset + FAT_SIZE_BYTES:
                rel_offset = offset - fat_start_offset
                end_offset = min(rel_offset + length, FAT_SIZE_BYTES)
                # print(f"DEBUG: Mock read_bytes returning FAT data slice (offset {offset}, rel {rel_offset}, len {length})")
                return bytes(self.fat_data[rel_offset:end_offset])
            # Read from root dir or data area (return zeros for simplicity here)
            # Root dir offset for test setup
            root_start = self.fs.root_dir_start_offset
            root_len = self.fs.root_dir_bytes
            if offset >= root_start and offset < root_start + root_len:
                 rel_offset = offset - root_start
                 end_offset = min(rel_offset + length, root_len)
                 # Simulate empty root dir initially
                 return b'\x00' * (end_offset - rel_offset)

            # print(f"DEBUG: Mock read_bytes returning ZEROS (offset {offset}, len {length})")
            return b'\x00' * length
        # Replace the default mock with this more specific one
        self.mock_disk._read_bytes = MagicMock(side_effect=read_bytes_side_effect)


        # Mock write_bytes used by FAT/Dir operations
        self.written_data = {} # Store writes {offset: data}
        def write_bytes_side_effect(offset, data):
            # print(f"DEBUG: Mock write_bytes (offset {offset}, len {len(data)})")
            self.written_data[offset] = bytes(data) # Record write
            # Simulate writing to both FATs if it's a FAT write
            fat_start_offset = self.fs.fat_start_offset
            fat_size_bytes = FAT_SIZE_BYTES
            if fat_start_offset <= offset < fat_start_offset + fat_size_bytes:
                 # Update internal FAT cache mock used by read_bytes_side_effect
                 rel_offset = offset - fat_start_offset
                 # Ensure slice indices are within bounds
                 write_end = min(rel_offset + len(data), len(self.fat_data))
                 if rel_offset < len(self.fat_data):
                      self.fat_data[rel_offset : write_end] = data[:write_end-rel_offset]

                 # Also record the mirrored write
                 fat2_offset = offset + fat_size_bytes
                 self.written_data[fat2_offset] = bytes(data)
                 # print(f"DEBUG: Mock write_bytes recorded FAT write at {offset} and {fat2_offset}")

        # Replace mock on disk instance
        self.mock_disk._write_bytes = MagicMock(side_effect=write_bytes_side_effect)

        # Ensure FS internal FAT cache is loaded using the mocked read_bytes
        self.fs.fat_cache = None # Clear any cache from initial setup
        self.assertTrue(self.fs._load_fat_cache(), "Failed to load FAT cache in setUp using mocks")

    def tearDown(self):
        print(f"--- Finished test: {self.id()} ---") # Simple teardown message


    def test_01_write_file_invalid_name(self):
        """Test write_file fails with invalid 8.3 filename"""
        invalid_names = [
            "BAD NAME.TXT",   # Space
            "TOOLONGNAM.TXT", # Name too long
            "FILE.TOOLONGEXT",# Ext too long
            "COM1",           # Reserved name
            ".BADNAME",       # Starts with dot
            "GOOD.",          # Ends with dot
            "INVALID*CHAR",   # Invalid character
        ]
        for name in invalid_names:
             with self.subTest(invalid_name=name):
                 with self.assertRaisesRegex(ValueError, "Invalid 8.3 filename",
                                             msg=f"Failed to raise ValueError for invalid name: {name}"):
                     self.fs.write_file(name, b"data")

    def test_02_create_dir_invalid_name(self):
        """Test create_directory fails with invalid 8.3 filename"""
        invalid_names = [
             "BAD NAME",       # Space
             "TOOLONGDIRNAME", # Name too long
             "DIR.EXT",        # Extension not allowed for dir
             "COM1",           # Reserved name
             ".BADDIR",        # Starts with dot
             "GOODDIR.",       # Ends with dot
             "INVALID?CHAR",   # Invalid character
        ]
        for name in invalid_names:
             with self.subTest(invalid_name=name):
                 with self.assertRaisesRegex(ValueError, "Invalid 8.3 directory name",
                                             msg=f"Failed to raise ValueError for invalid name: {name}"):
                     self.fs.create_directory(name)

    def test_03_delete_non_empty_directory(self):
        """Test deleting a non-empty directory fails"""
        dir_name_to_delete = "DELDIR"
        dir_path_to_delete = f"/{dir_name_to_delete}"
        dir_cluster = 5 # Assume this cluster for the mock dir
        # FIX: Use correct attribute name
        root_dir_start_offset = self.fs.root_dir_start_offset
        root_dir_read_len = self.fs.root_dir_bytes # Use calculated bytes

        # --- Mock Setup ---
        # 1. Mock _list_directory_by_cluster to indicate non-emptiness
        mock_file_entry = FileInfo(name="dummy.txt", size=10, is_dir=False, datetime=datetime.datetime.now(), attributes="-A-", starting_cluster=6)
        # Ensure the patch targets the instance self.fs
        patch_list_cluster = patch.object(self.fs, '_list_directory_by_cluster', return_value=[mock_file_entry])
        mock_list_cluster_func = patch_list_cluster.start()
        self.addCleanup(patch_list_cluster.stop) # Ensure patch stops

        # 2. Create the directory entry bytes we expect delete to find
        mock_dir_entry_info = FileInfo(name=dir_name_to_delete, size=0, is_dir=True, datetime=datetime.datetime.now(), attributes="-D-", starting_cluster=dir_cluster)
        # Use the correct bytes creation method
        deldir_entry_bytes = self.fs._create_directory_entry_bytes(
            name=dir_name_to_delete,
            is_dir=True,
            starting_cluster=dir_cluster,
            size=0,
            dt=mock_dir_entry_info.datetime
        )

        # 3. Mock _read_bytes *specifically for this test* to provide the root dir entry
        original_read_bytes = self.fs._read_bytes # Keep original if needed elsewhere
        def read_bytes_for_delete_test(offset, length):
             # print(f"DEBUG DeleteTest: _read_bytes called with offset={offset}, length={length}")
             if offset == root_dir_start_offset and length >= len(deldir_entry_bytes): # Check offset and plausible length
                  # print(f"DEBUG DeleteTest: Returning root with crafted DELDIR entry at start.")
                  buffer = bytearray(length) # Create buffer of requested length
                  buffer[:len(deldir_entry_bytes)] = deldir_entry_bytes
                  # print(f"DEBUG DeleteTest: Returning root buffer (first 64): {bytes(buffer[:64]).hex(' ')}")
                  return bytes(buffer)
             # Fallback for FAT reads etc., using the setUp's mock logic
             fat_start = self.fs.fat_start_offset
             if fat_start <= offset < fat_start + FAT_SIZE_BYTES:
                 rel_offset = offset - fat_start
                 end_offset = min(rel_offset + length, FAT_SIZE_BYTES)
                 return bytes(self.fat_data[rel_offset:end_offset])
             # print(f"DEBUG DeleteTest: Falling back to ZEROS for offset {offset}")
             return b'\x00' * length # Default zeros otherwise
        # Patch directly on the instance for this test
        patch_read_bytes = patch.object(self.fs, '_read_bytes', side_effect=read_bytes_for_delete_test)
        mock_read_bytes_func = patch_read_bytes.start()
        self.addCleanup(patch_read_bytes.stop) # Stop this specific patch

        # --- Execute Test ---
        # print(f"DEBUG DeleteTest: Calling self.fs.delete('{dir_path_to_delete}')")
        # Expect OSError now
        with self.assertRaisesRegex(OSError, "Directory not empty", msg="Delete should fail because directory is not empty"):
             self.fs.delete(dir_path_to_delete)

        # --- Assertions ---
        # Verify _read_bytes was called for the root directory
        mock_read_bytes_func.assert_any_call(root_dir_start_offset, root_dir_read_len)
        # Verify _list_directory_by_cluster was called to check emptiness
        mock_list_cluster_func.assert_called_once_with(dir_cluster)


    def test_09_write_no_free_clusters(self):
        """Test write_file fails when no free clusters are available"""
        with patch.object(self.fs, '_find_free_cluster', return_value=None):
             # Need to mock _find_path to simulate finding the parent dir
             # If parent is root, it needs cluster 0, which is handled internally
             with patch.object(self.fs, '_find_path', return_value=None) as mock_find_path:
                 # FIX: Expect OSError or IOError (subclass of OSError)
                 with self.assertRaisesRegex(OSError, "Not enough free space on disk"):
                     self.fs.write_file("/TEST.TXT", b"some data")


    def test_10_write_no_free_dir_entry_root(self):
        """Test write_file fails when root directory is full"""
        # Mock _find_free_directory_entry for root (cluster 0) to return None
        with patch.object(self.fs, '_find_free_directory_entry', lambda cluster: None if cluster == 0 else (5, 0)): # Return dummy for non-root
             with patch.object(self.fs, '_find_path', return_value=None): # Simulate writing to root
                 # Need to mock cluster allocation to succeed initially
                 with patch.object(self.fs, '_allocate_cluster_chain', return_value=[5]):
                     # Mock the cached free method used in cleanup
                     with patch.object(self.fs, '_free_cluster_chain') as mock_free:
                          # FIX: Expect OSError or IOError
                          with self.assertRaisesRegex(OSError, "No space in directory /"):
                              self.fs.write_file("/TEST.TXT", b"data")
                          # Ensure allocated cluster was freed (passed to internal method)
                          mock_free.assert_called_once_with(5)


    def test_11_write_no_free_dir_entry_subdir(self):
        """Test write_file fails when subdirectory cannot be extended"""
        # Simulate finding the subdir entry
        mock_dir_entry = FileInfo(name="SUB", size=0, is_dir=True, datetime=datetime.datetime.now(), attributes="-D-", starting_cluster=5)
        # Mock _find_path to return the dir entry for /SUB, and None otherwise
        def mock_find_path(path):
            if self.fs._normalize_path(path) == "/SUB": return mock_dir_entry
            # Simulate root existing for parent check
            if self.fs._normalize_path(path) == "/": return FileInfo("/", 0, True, datetime.datetime.now(), "D", 0)
            return None
        with patch.object(self.fs, '_find_path', side_effect=mock_find_path):
            # Mock _find_free_directory_entry to fail for the subdir's cluster
            with patch.object(self.fs, '_find_free_directory_entry', lambda cluster: None if cluster == 5 else (10, 0)): # Fail only for cluster 5
                # Mock cluster allocation to succeed initially
                with patch.object(self.fs, '_allocate_cluster_chain', return_value=[6]):
                    with patch.object(self.fs, '_free_cluster_chain') as mock_free:
                         # FIX: Expect OSError or IOError
                        with self.assertRaisesRegex(OSError, "No space in directory /SUB"):
                            self.fs.write_file("/SUB/TEST.TXT", b"data")
                        mock_free.assert_called_once_with(6)

    def test_12_create_dir_no_free_clusters(self):
        """Test create_directory fails when no free clusters are available"""
        with patch.object(self.fs, '_find_path', return_value=None): # Simulate creating in root
             with patch.object(self.fs, '_find_free_cluster', return_value=None):
                  # FIX: Expect OSError or IOError
                  with self.assertRaisesRegex(OSError, "No free clusters available"):
                      self.fs.create_directory("/NEWDIR")


    def test_13_create_dir_no_free_entry(self):
        """Test create_directory fails when parent directory is full"""
        with patch.object(self.fs, '_find_path', return_value=None): # Simulate creating in root
             # Mock find_free_directory_entry for root (cluster 0) to return None
             with patch.object(self.fs, '_find_free_directory_entry', lambda cluster: None if cluster == 0 else (10, 0)): # Fail for root
                 # Mock cluster allocation to succeed initially
                 with patch.object(self.fs, '_find_free_cluster', return_value=5):
                      # FIX: Mock the cached set method used in cleanup
                      with patch.object(self.fs, '_set_fat_entry_cached') as mock_set_fat_cached:
                          # FIX: Expect OSError or IOError
                          with self.assertRaisesRegex(OSError, "No space in parent directory"):
                              self.fs.create_directory("/NEWDIR")
                          # Check that the allocated cluster (5) was freed (set to 0) via cache method
                          mock_set_fat_cached.assert_any_call(5, 0)


    def test_14_fat12_odd_cluster_read_write(self):
        """Test reading/writing FAT entry for an odd cluster number"""
        cluster = 3 # Odd cluster
        byte_offset = int(cluster * 1.5) # 3 * 1.5 = 4.5 -> 4

        # Simulate existing value in the 2 bytes at offset 4
        initial_word = 0xDEF3
        # Ensure the test's fat_data reflects this before writing
        struct.pack_into('<H', self.fat_data, byte_offset, initial_word)
        # Ensure the filesystem's internal cache matches our test data
        self.fs.fat_cache = self.fat_data[:] # Make a copy

        # --- Test Write ---
        value_to_write = 0xABC
        expected_word = (initial_word & 0x000F) | (value_to_write << 4) # 0xABC3

        # FIX: Call _set_fat_entry_cached
        self.fs._set_fat_entry_cached(cluster, value_to_write)

        # Verify internal fat_cache (using self.fs.fat_cache)
        written_word = struct.unpack_from('<H', self.fs.fat_cache, byte_offset)[0]
        self.assertEqual(written_word, expected_word)

        # --- Test Read ---
        # Set fs cache to the target word for read test
        self.fs.fat_cache = bytearray(FAT_SIZE_BYTES) # Clear cache
        struct.pack_into('<H', self.fs.fat_cache, byte_offset, expected_word) # Set word to read

        # FIX: Call _read_fat_entry_cached
        read_value = self.fs._read_fat_entry_cached(cluster)
        self.assertEqual(read_value, value_to_write)


    def test_15_fat12_even_cluster_read_write(self):
        """Test reading/writing FAT entry for an even cluster number"""
        cluster = 2 # Even cluster
        byte_offset = int(cluster * 1.5) # 2 * 1.5 = 3

        # Simulate existing value in the 2 bytes at offset 3
        initial_word = 0xDE23
        struct.pack_into('<H', self.fat_data, byte_offset, initial_word)
        self.fs.fat_cache = self.fat_data[:]

        # --- Test Write ---
        value_to_write = 0x321
        expected_word = (initial_word & 0xF000) | (value_to_write & 0x0FFF) # 0xD321

        # FIX: Call _set_fat_entry_cached
        self.fs._set_fat_entry_cached(cluster, value_to_write)

        written_word = struct.unpack_from('<H', self.fs.fat_cache, byte_offset)[0]
        self.assertEqual(written_word, expected_word)

        # --- Test Read ---
        self.fs.fat_cache = bytearray(FAT_SIZE_BYTES)
        struct.pack_into('<H', self.fs.fat_cache, byte_offset, expected_word)

        # FIX: Call _read_fat_entry_cached
        read_value = self.fs._read_fat_entry_cached(cluster)
        self.assertEqual(read_value, value_to_write)


    def test_16_find_free_cluster(self):
        """Test _find_free_cluster logic"""
        # Ensure filesystem's cache matches our test data
        self.fs.fat_cache = self.fat_data[:]

        # Simulate clusters 2, 3 used, 4 free
        # FIX: Call _set_fat_entry_cached
        self.fs._set_fat_entry_cached(2, 0xFFF)
        self.fs._set_fat_entry_cached(3, 0xFFF)
        self.fs._set_fat_entry_cached(4, 0)

        free_cluster = self.fs._find_free_cluster()
        self.assertEqual(free_cluster, 4)

        # Mark 4 as used and check again
        self.fs._set_fat_entry_cached(4, 0xFFF)
        free_cluster = self.fs._find_free_cluster()
        # Cluster 5 should be the next free one by default
        self.assertEqual(free_cluster, 5)


    def test_17_find_free_cluster_none_free(self):
        """Test _find_free_cluster when disk is full"""
        self.fs.fat_cache = self.fat_data[:]

        # Mark all clusters as used
        for i in range(2, self.fs.num_clusters + 2):
             # FIX: Call _set_fat_entry_cached
             self.fs._set_fat_entry_cached(i, 0xFFF)

        free_cluster = self.fs._find_free_cluster()
        self.assertIsNone(free_cluster)

    def test_18_fs_multi_sector_read_write_edge(self):
        """Test _read_bytes and _write_bytes across sector boundaries"""
        sector_size = self.fs.boot_sector.bytes_per_sector
        test_offset = sector_size - 50 # Start 50 bytes before end of sector 0
        test_len = 150 # Read/Write 150 bytes (50 in sector 0, 100 in sector 1)
        test_data = bytes([i % 256 for i in range(test_len)])

        # Mock the underlying disk methods more comprehensively
        written_sectors = {} # Store written data C:H:S -> bytes

        def mock_write_sector(c, h, s, data):
            # print(f"DEBUG mock_write_sector: C:{c} H:{h} S:{s} Len:{len(data)}")
            written_sectors[f"{c}:{h}:{s}"] = bytes(data)

        def mock_read_sector(c, h, s):
            key = f"{c}:{h}:{s}"
            # print(f"DEBUG mock_read_sector: C:{c} H:{h} S:{s} -> Found: {key in written_sectors}")
            return written_sectors.get(key, b'\x00' * sector_size)

        def mock_read_sectors(start_c, start_h, start_s, num_sectors):
            # Simulate multi-sector read based on single sector mock
            # print(f"DEBUG mock_read_sectors: C:{start_c} H:{start_h} S:{start_s} Num:{num_sectors}")
            result = bytearray()
            c, h, s = start_c, start_h, start_s
            geom = self.fs.disk.geometry
            for _ in range(num_sectors):
                 if not (0 <= c < geom.cylinders and 0 <= h < geom.heads and 1 <= s <= geom.sectors_per_track):
                     # Simulate short read if out of bounds
                     print(f"WARN mock_read_sectors: calculated OOB C:{c} H:{h} S:{s}")
                     break
                 result.extend(mock_read_sector(c, h, s))
                 # Increment CHS
                 s += 1
                 if s > geom.sectors_per_track:
                     s = 1; h += 1
                     if h >= geom.heads:
                         h = 0; c += 1
            # print(f"DEBUG mock_read_sectors returning {len(result)} bytes")
            return bytes(result)

        # We need to patch the methods on the *mock_disk* instance used by self.fs
        with patch.object(self.fs.disk, 'read_sector', side_effect=mock_read_sector), \
             patch.object(self.fs.disk, 'write_sector', side_effect=mock_write_sector), \
             patch.object(self.fs.disk, 'read_sectors', side_effect=mock_read_sectors): # Mock read_sectors too

            # Write data spanning sectors using the filesystem's _write_bytes
            self.fs._write_bytes(test_offset, test_data)

            # Calculate expected CHS (use fs helper)
            start_lba = test_offset // sector_size
            end_lba = (test_offset + test_len - 1) // sector_size
            cyl1, head1, sect1 = self.fs._lba_to_chs(start_lba)
            cyl2, head2, sect2 = self.fs._lba_to_chs(end_lba)
            key1 = f"{cyl1}:{head1}:{sect1}"
            key2 = f"{cyl2}:{head2}:{sect2}"

            # Verify write_sector was called via _write_bytes logic
            self.assertIn(key1, written_sectors, f"Sector {key1} not written")
            if start_lba != end_lba: # Only check second sector if span occurred
                 self.assertIn(key2, written_sectors, f"Sector {key2} not written")

            # Verify data placement within the mocked sectors
            bytes_in_first = sector_size - (test_offset % sector_size)
            bytes_in_second = test_len - bytes_in_first
            self.assertEqual(written_sectors[key1][test_offset % sector_size:], test_data[:bytes_in_first], "Data mismatch in first sector")
            if start_lba != end_lba and bytes_in_second > 0:
                self.assertEqual(written_sectors[key2][:bytes_in_second], test_data[bytes_in_first:], "Data mismatch in second sector")

            # Read back the data using _read_bytes, which should now use mock_read_sectors
            read_back_data = self.fs._read_bytes(test_offset, test_len)
            # FIX: Compare actual data, not mock object
            self.assertEqual(read_back_data, test_data, "Read back data does not match original test data")


    def test_19_fs_parse_corrupt_entry(self):
        """Test parsing specifically crafted corrupt directory entries"""
        base_entry = self.fs._create_directory_entry_bytes("GOODFILE.TXT", False, 5, 100, datetime.datetime.now())

        # Test case 1: Invalid characters in name
        entry_bad_char = bytearray(base_entry)
        entry_bad_char[0] = ord('*') # Invalid char
        self.assertIsNone(self.fs._parse_single_directory_entry(bytes(entry_bad_char)))

        # Test case 2: Invalid date/time (e.g., month 13) - should default
        entry_bad_date = bytearray(base_entry)
        # date_val format: YYYYYYYMMMMDDDDD
        year_part = (2020 - 1980) << 9
        month_part = 13 << 5 # Invalid month
        day_part = 1
        date_val = year_part | month_part | day_part
        struct.pack_into('<H', entry_bad_date, 24, date_val)
        parsed_bad_date = self.fs._parse_single_directory_entry(bytes(entry_bad_date))
        self.assertIsNotNone(parsed_bad_date)
        self.assertEqual(parsed_bad_date.datetime.year, 1980) # Check default year
        self.assertEqual(parsed_bad_date.datetime.month, 1)   # Check default month
        self.assertEqual(parsed_bad_date.datetime.day, 1)     # Check default day

        # Test case 3: LFN entry attribute (should be skipped)
        entry_lfn = bytearray(base_entry)
        entry_lfn[11] = 0x0F # LFN attribute (using constant is better: ATTR_LONG_NAME)
        self.assertIsNone(self.fs._parse_single_directory_entry(bytes(entry_lfn)))

        # Test case 4: Volume ID attribute (should be parsed with VOL attribute)
        entry_volid = bytearray(base_entry)
        # FIX: Overwrite all 11 name/ext bytes correctly for volume label
        vol_label_padded = b'VOL LABEL'.ljust(11) # Pad with spaces
        entry_volid[0:11] = vol_label_padded      # Set all 11 bytes
        entry_volid[11] = 0x08 # Volume ID attribute (using constant: ATTR_VOLUME_ID)
        parsed_vol = self.fs._parse_single_directory_entry(bytes(entry_volid))
        self.assertIsNotNone(parsed_vol)
        # Now the name should parse correctly as spaces are ignored
        self.assertEqual(parsed_vol.name, "VOL LABEL")
        self.assertEqual(parsed_vol.attributes, "VOL")


if __name__ == '__main__':
    unittest.main()
