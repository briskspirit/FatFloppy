# tests/test_07_filesystem_fat_errors.py
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import os
import sys
import datetime

# Ensure src is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from fatfloppy.core.filesystem import FATFilesystem, FATBootSector, FileInfo
from fatfloppy.core.disk import Disk, DiskGeometry
from fatfloppy.core.drivers import DiskIODriver # Use generic driver mock
import struct # Import struct

# Minimal valid BPB for FAT12 (e.g., 720k-like)
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
# Adjust num_clusters calculation based on actual start sector
NUM_CLUSTERS = (TOTAL_SECTORS - DATA_AREA_START_SECTOR) // SECTORS_PER_CLUSTER

class TestFATFilesystemErrors(unittest.TestCase):

    def setUp(self):
        self.mock_driver = MagicMock(spec=DiskIODriver)
        self.mock_disk = MagicMock(spec=Disk)
        self.mock_disk.driver = self.mock_driver
        # Set up geometry consistent with BPB
        self.mock_disk_geometry = DiskGeometry( # Store geometry separately
            cylinders=TOTAL_SECTORS // (SECTORS_PER_TRACK * NUM_HEADS),
            heads=NUM_HEADS,
            sectors_per_track=SECTORS_PER_TRACK,
            sector_size=SECTOR_SIZE
        )
        # Use PropertyMock correctly
        type(self.mock_disk).geometry = PropertyMock(return_value=self.mock_disk_geometry)

        # Create a mock boot sector
        self.mock_boot_sector_data = bytearray(SECTOR_SIZE)
        struct.pack_into('<H', self.mock_boot_sector_data, 0x0B, SECTOR_SIZE)
        self.mock_boot_sector_data[0x0D] = SECTORS_PER_CLUSTER
        struct.pack_into('<H', self.mock_boot_sector_data, 0x0E, RESERVED_SECTORS)
        self.mock_boot_sector_data[0x10] = NUM_FATS
        struct.pack_into('<H', self.mock_boot_sector_data, 0x11, ROOT_ENTRIES)
        struct.pack_into('<H', self.mock_boot_sector_data, 0x13, TOTAL_SECTORS) # Small disk
        self.mock_boot_sector_data[0x15] = 0xF9 # Media desc
        struct.pack_into('<H', self.mock_boot_sector_data, 0x16, SECTORS_PER_FAT)
        struct.pack_into('<H', self.mock_boot_sector_data, 0x18, SECTORS_PER_TRACK)
        struct.pack_into('<H', self.mock_boot_sector_data, 0x1A, NUM_HEADS)
        struct.pack_into('<I', self.mock_boot_sector_data, 0x1C, 0) # Hidden
        struct.pack_into('<H', self.mock_boot_sector_data, 0x1FE, 0xAA55) # Signature

        # Mock read_sector to return the boot sector when C=0,H=0,S=1
        def mock_read_sector(c, h, s):
             if c == 0 and h == 0 and s == 1:
                 # print("DEBUG Mock read_sector: Returning boot sector")
                 return bytes(self.mock_boot_sector_data)
             # print(f"DEBUG Mock read_sector: Returning zeros for C:{c} H:{h} S:{s}")
             return bytes(SECTOR_SIZE) # Return zeros for other sectors
        self.mock_disk.read_sector = MagicMock(side_effect=mock_read_sector)


        # Mock _read_bytes used by FAT operations - return zeros initially
        # We'll override side_effect in tests needing specific FAT data
        self.mock_disk._read_bytes = MagicMock(return_value=b'\x00' * 4096) # Default return value

        # Initialize filesystem AFTER mocks are set up
        self.fs = FATFilesystem(self.mock_disk)
        self.assertTrue(self.fs.is_valid())

        # --- Setup for FAT operations mocking ---
        self.fat_data = bytearray(FAT_SIZE_BYTES) # Size of ONE FAT copy
        # FAT ID bytes
        self.fat_data[0] = 0xF9
        self.fat_data[1] = 0xFF
        self.fat_data[2] = 0xFF
        # Set up mock to return FAT data when asked
        def read_bytes_side_effect(offset, length):
            fat_start_offset = self.fs.fat_start
            # Check if read is within the first FAT copy
            if offset >= fat_start_offset and offset < fat_start_offset + FAT_SIZE_BYTES:
                rel_offset = offset - fat_start_offset
                end_offset = min(rel_offset + length, FAT_SIZE_BYTES)
                # print(f"DEBUG: Mock read_bytes returning FAT data slice (offset {offset}, rel {rel_offset}, len {length})")
                return bytes(self.fat_data[rel_offset:end_offset])
            # Read from root dir or data area (return zeros for simplicity here)
            # print(f"DEBUG: Mock read_bytes returning ZEROS (offset {offset}, len {length})")
            return b'\x00' * length
        self.mock_disk._read_bytes.side_effect = read_bytes_side_effect

        # Mock write_bytes used by FAT/Dir operations
        self.written_data = {} # Store writes {offset: data}
        def write_bytes_side_effect(offset, data):
            # print(f"DEBUG: Mock write_bytes (offset {offset}, len {len(data)})")
            self.written_data[offset] = bytes(data)
            # Simulate writing to both FATs if it's a FAT write
            fat_start_offset = self.fs.fat_start
            fat_size_bytes = FAT_SIZE_BYTES
            if offset >= fat_start_offset and offset < fat_start_offset + fat_size_bytes:
                 # Update internal FAT cache mock used by read_bytes_side_effect
                 rel_offset = offset - fat_start_offset
                 self.fat_data[rel_offset : rel_offset + len(data)] = data
                 # Also record the mirrored write
                 fat2_offset = offset + fat_size_bytes
                 self.written_data[fat2_offset] = bytes(data)
                 # print(f"DEBUG: Mock write_bytes recorded FAT write at {offset} and {fat2_offset}")

        self.mock_disk._write_bytes = MagicMock(side_effect=write_bytes_side_effect)

        # Ensure FAT cache is loaded for tests modifying it
        if self.fs.fat_cache is None:
             self.fs._read_fat_sectors() # Load the cache using the mocked _read_bytes


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
             # Use subtest for better error reporting
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
        root_dir_start_offset = self.fs.root_dir_start
        root_dir_read_len = self.fs.boot_sector.root_entries * 32

        # --- Mock Setup ---
        # 1. Mock _list_directory_by_cluster to indicate non-emptiness
        mock_file_entry = FileInfo(name="dummy.txt", size=10, is_dir=False, datetime=datetime.datetime.now(), attributes="-", starting_cluster=6)
        patch_list_cluster = patch.object(self.fs, '_list_directory_by_cluster', return_value=[mock_file_entry])
        mock_list_cluster_func = patch_list_cluster.start()
        self.addCleanup(patch_list_cluster.stop) # Ensure patch stops

        # 2. Mock find_path (only needed if delete checks parent existence differently, but let's keep it simple)
        #    Delete primarily relies on scanning the parent dir bytes.

        # 3. Create the directory entry bytes we expect delete to find
        mock_dir_entry_info = FileInfo(name=dir_name_to_delete, size=0, is_dir=True, datetime=datetime.datetime.now(), attributes="-", starting_cluster=dir_cluster)
        deldir_entry_bytes = self.fs._create_directory_entry(
            name=dir_name_to_delete,
            is_dir=True,
            starting_cluster=dir_cluster,
            size=0,
            dt=mock_dir_entry_info.datetime
        )

        # 4. Mock _read_bytes *specifically for this test* to provide the root dir entry
        original_read_bytes = self.fs._read_bytes # Keep original if needed elsewhere
        def read_bytes_for_delete_test(offset, length):
             print(f"DEBUG DeleteTest: _read_bytes called with offset={offset}, length={length}")
             # Is it reading the root directory sector(s)?
             if offset == root_dir_start_offset and length == root_dir_read_len:
                  print(f"DEBUG DeleteTest: Returning root with crafted DELDIR entry at start.")
                  buffer = bytearray(length)
                  buffer[:len(deldir_entry_bytes)] = deldir_entry_bytes
                  # --- Debug: Print the exact bytes being returned ---
                  print(f"DEBUG DeleteTest: Returning root buffer (first 64): {bytes(buffer[:64]).hex(' ')}")
                  # ---
                  return bytes(buffer)
             else:
                  # Fallback for other reads (like FAT reads if needed by delete logic)
                  print(f"DEBUG DeleteTest: Falling back to original _read_bytes for offset {offset}")
                  # We might need to actually return the FAT data here if delete checks it
                  fat_start_offset = self.fs.fat_start
                  fat_size_bytes = FAT_SIZE_BYTES
                  if offset >= fat_start_offset and offset < fat_start_offset + fat_size_bytes:
                      rel_offset = offset - fat_start_offset
                      end_offset = min(rel_offset + length, fat_size_bytes)
                      return bytes(self.fat_data[rel_offset:end_offset])
                  return b'\x00' * length # Default zeros otherwise
        # Patch directly on the instance for this test
        patch_read_bytes = patch.object(self.fs, '_read_bytes', side_effect=read_bytes_for_delete_test)
        mock_read_bytes_func = patch_read_bytes.start()
        self.addCleanup(patch_read_bytes.stop)

        # --- Execute Test ---
        print(f"DEBUG DeleteTest: Calling self.fs.delete('{dir_path_to_delete}')")
        # We expect the "Directory not empty" error
        with self.assertRaisesRegex(ValueError, "Directory not empty", msg="Delete should fail because directory is not empty"):
             self.fs.delete(dir_path_to_delete)

        # --- Assertions ---
        # Verify _read_bytes was called for the root directory
        mock_read_bytes_func.assert_any_call(root_dir_start_offset, root_dir_read_len)
        # Verify _list_directory_by_cluster was called to check emptiness
        mock_list_cluster_func.assert_called_once_with(dir_cluster)


    def test_09_write_no_free_clusters(self): # Renamed from test_04
        """Test write_file fails when no free clusters are available"""
        # Mock _find_free_cluster to return None
        with patch.object(self.fs, '_find_free_cluster', return_value=None):
             # Ensure _find_path returns None for root (so it writes to root)
             with patch.object(self.fs, '_find_path', return_value=None):
                 with self.assertRaisesRegex(ValueError, "Not enough free space on disk"):
                     self.fs.write_file("/TEST.TXT", b"some data")


    def test_10_write_no_free_dir_entry_root(self): # Renamed from test_05
        """Test write_file fails when root directory is full"""
        # Mock _find_free_directory_entry for root (cluster 0) to return None
        with patch.object(self.fs, '_find_free_directory_entry', lambda cluster: None if cluster == 0 else 1234):
             # Ensure _find_path returns None for root
             with patch.object(self.fs, '_find_path', return_value=None):
                 # Need to mock cluster allocation to succeed initially
                 with patch.object(self.fs, '_allocate_cluster_chain', return_value=[5]):
                     with patch.object(self.fs, '_free_cluster_chain') as mock_free: # Check cleanup
                          with self.assertRaisesRegex(ValueError, "No space in directory"):
                              self.fs.write_file("/TEST.TXT", b"data")
                          mock_free.assert_called_once_with(5) # Ensure allocated cluster was freed


    def test_11_write_no_free_dir_entry_subdir(self):
        """Test write_file fails when subdirectory cannot be extended"""
        # Simulate finding the subdir entry
        mock_dir_entry = FileInfo(name="SUB", size=0, is_dir=True, datetime=datetime.datetime.now(), attributes="-", starting_cluster=5)
        with patch.object(self.fs, '_find_path', side_effect=lambda p: mock_dir_entry if p == "/SUB" else None):
            # Mock _find_free_directory_entry to fail for the subdir's cluster chain
            with patch.object(self.fs, '_find_free_directory_entry', lambda cluster: None if cluster == 5 else 1234):
                # Mock cluster allocation to succeed initially
                with patch.object(self.fs, '_allocate_cluster_chain', return_value=[6]):
                    with patch.object(self.fs, '_free_cluster_chain') as mock_free:
                        with self.assertRaisesRegex(ValueError, "No space in directory"):
                            self.fs.write_file("/SUB/TEST.TXT", b"data")
                        mock_free.assert_called_once_with(6)

    def test_12_create_dir_no_free_clusters(self): # Renamed from test_06
        """Test create_directory fails when no free clusters are available"""
        # Ensure _find_path returns None for root
        with patch.object(self.fs, '_find_path', return_value=None):
             with patch.object(self.fs, '_find_free_cluster', return_value=None):
                  with self.assertRaisesRegex(ValueError, "No free clusters available"):
                      self.fs.create_directory("/NEWDIR")


    def test_13_create_dir_no_free_entry(self):
        """Test create_directory fails when parent directory is full"""
        # Ensure _find_path returns None for root
        with patch.object(self.fs, '_find_path', return_value=None):
             # Mock find_free_directory_entry for root (cluster 0) to return None
             with patch.object(self.fs, '_find_free_directory_entry', lambda cluster: None if cluster == 0 else 1234):
                 # Mock cluster allocation to succeed initially
                 with patch.object(self.fs, '_find_free_cluster', return_value=5):
                      # We need to mock _set_fat_entry because the cleanup calls it
                      with patch.object(self.fs, '_set_fat_entry') as mock_set_fat:
                          with self.assertRaisesRegex(ValueError, "No space in parent directory"):
                              self.fs.create_directory("/NEWDIR")
                          # Check that the allocated cluster (5) was attempted to be freed (set to 0)
                          # _set_fat_entry is called multiple times during create_dir setup AND cleanup
                          # We specifically want to see the cleanup call: _set_fat_entry(5, 0)
                          mock_set_fat.assert_any_call(5, 0)


    def test_14_fat12_odd_cluster_read_write(self): # Renamed from test_07
        """Test reading/writing FAT entry for an odd cluster number"""
        cluster = 3 # Odd cluster
        byte_offset = int(cluster * 1.5) # 3 * 1.5 = 4.5 -> 4

        # --- Test Write ---
        value_to_write = 0xABC
        # Simulate existing value in the 2 bytes at offset 4 (little endian)
        initial_word = 0xDEF3
        struct.pack_into('<H', self.fat_data, byte_offset, initial_word)
        # Ensure the filesystem's internal cache reflects this before writing
        self.fs.fat_cache = self.fat_data[:] # Make a copy

        # Expected word after writing 0xABC to cluster 3 (odd)
        expected_word = (initial_word & 0x000F) | (value_to_write << 4) # 0xABC3

        self.fs._set_fat_entry_mem(cluster, value_to_write)

        # Verify internal fat_data cache (using self.fs.fat_cache)
        written_word = struct.unpack_from('<H', self.fs.fat_cache, byte_offset)[0]
        self.assertEqual(written_word, expected_word)

        # --- Test Read ---
        # Set fs cache back to the calculated word for read test
        self.fs.fat_cache = bytearray(FAT_SIZE_BYTES) # Clear cache
        struct.pack_into('<H', self.fs.fat_cache, byte_offset, expected_word) # Set word to read

        read_value = self.fs._read_fat_entry_mem(cluster)
        self.assertEqual(read_value, value_to_write)


    def test_15_fat12_even_cluster_read_write(self): # Renamed from test_08
        """Test reading/writing FAT entry for an even cluster number"""
        cluster = 2 # Even cluster
        byte_offset = int(cluster * 1.5) # 2 * 1.5 = 3

        # --- Test Write ---
        value_to_write = 0x321
        # Simulate existing value in the 2 bytes at offset 3
        initial_word = 0xDE23
        struct.pack_into('<H', self.fat_data, byte_offset, initial_word)
        # Ensure the filesystem's internal cache reflects this before writing
        self.fs.fat_cache = self.fat_data[:] # Make a copy

        # Expected word after writing 0x321 to cluster 2 (even)
        expected_word = (initial_word & 0xF000) | (value_to_write & 0x0FFF) # 0xD321

        self.fs._set_fat_entry_mem(cluster, value_to_write)

        # Verify internal fat_data cache (using self.fs.fat_cache)
        written_word = struct.unpack_from('<H', self.fs.fat_cache, byte_offset)[0]
        self.assertEqual(written_word, expected_word)

        # --- Test Read ---
        # Set fs cache back to the calculated word for read test
        self.fs.fat_cache = bytearray(FAT_SIZE_BYTES) # Clear cache
        struct.pack_into('<H', self.fs.fat_cache, byte_offset, expected_word) # Set word to read

        read_value = self.fs._read_fat_entry_mem(cluster)
        self.assertEqual(read_value, value_to_write)


    # Add tests for Gap 6 (FAT Allocation details)
    def test_16_find_free_cluster(self):
        """Test _find_free_cluster logic"""
        # Ensure FAT cache is loaded and reflects self.fat_data
        self.fs.fat_cache = self.fat_data[:]

        # Clusters 0, 1 reserved. Start check at 2.
        # Simulate clusters 2, 3 used, 4 free
        self.fs._set_fat_entry_mem(2, 0xFFF) # Mark 2 as used (EOF)
        self.fs._set_fat_entry_mem(3, 0xFFF) # Mark 3 as used (EOF)
        self.fs._set_fat_entry_mem(4, 0)     # Mark 4 as free

        free_cluster = self.fs._find_free_cluster()
        self.assertEqual(free_cluster, 4)

        # Mark 4 as used and check again, should find next (default is 0 -> cluster 5)
        self.fs._set_fat_entry_mem(4, 0xFFF)
        free_cluster = self.fs._find_free_cluster()
        self.assertEqual(free_cluster, 5)


    def test_17_find_free_cluster_none_free(self):
        """Test _find_free_cluster when disk is full"""
        # Ensure FAT cache is loaded and reflects self.fat_data
        self.fs.fat_cache = self.fat_data[:]

        # Mark all clusters as used
        for i in range(2, self.fs.num_clusters + 2):
             self.fs._set_fat_entry_mem(i, 0xFFF)

        free_cluster = self.fs._find_free_cluster()
        self.assertIsNone(free_cluster)

    def test_18_fs_multi_sector_read_write_edge(self):
        """Test _read_bytes and _write_bytes across sector boundaries"""
        sector_size = self.fs.boot_sector.bytes_per_sector
        test_offset = sector_size - 50 # Start 50 bytes before end of first data sector
        test_len = 150 # Read/Write 150 bytes (spanning 2 sectors)
        # Create some test data for the span
        test_data = bytes([i % 256 for i in range(test_len)])

        # Mock the underlying disk read/write sector methods
        # Store written data per sector C:H:S -> bytes
        written_sectors = {}
        def mock_write_sector(c, h, s, data):
            written_sectors[f"{c}:{h}:{s}"] = bytes(data)
        def mock_read_sector(c, h, s):
            key = f"{c}:{h}:{s}"
            # Return previously written data or zeros
            return written_sectors.get(key, b'\x00' * sector_size)

        with patch.object(self.fs.disk, 'read_sector', side_effect=mock_read_sector):
            with patch.object(self.fs.disk, 'write_sector', side_effect=mock_write_sector):
                # Write data spanning sectors
                self.fs._write_bytes(test_offset, test_data)

                # Verify write_sector was called for the correct sectors
                # Calculate expected CHS for test_offset and test_offset+test_len
                cyl1, head1, sect1 = self.fs._lba_to_chs(test_offset // sector_size)
                cyl2, head2, sect2 = self.fs._lba_to_chs((test_offset + test_len - 1) // sector_size)
                key1 = f"{cyl1}:{head1}:{sect1}"
                key2 = f"{cyl2}:{head2}:{sect2}"
                self.assertIn(key1, written_sectors)
                self.assertIn(key2, written_sectors)
                # Verify data was placed correctly within sectors (more detailed checks possible)
                self.assertEqual(written_sectors[key1][test_offset % sector_size:], test_data[:sector_size - (test_offset % sector_size)])
                self.assertEqual(written_sectors[key2][:(test_len - (sector_size - (test_offset % sector_size)))], test_data[sector_size - (test_offset % sector_size):])


                # Read back the data using _read_bytes
                read_back_data = self.fs._read_bytes(test_offset, test_len)
                self.assertEqual(read_back_data, test_data)

    def test_19_fs_parse_corrupt_entry(self):
        """Test parsing specifically crafted corrupt directory entries"""
        base_entry = self.fs._create_directory_entry("GOODFILE.TXT", False, 5, 100, datetime.datetime.now())

        # Test case 1: Invalid characters in name
        entry_bad_char = bytearray(base_entry)
        entry_bad_char[0] = ord('*') # Invalid char
        self.assertIsNone(self.fs._parse_directory_entry(bytes(entry_bad_char)))

        # Test case 2: Invalid date/time (e.g., month 13) - should default
        entry_bad_date = bytearray(base_entry)
        date_val = (((2020 - 1980) & 0x7F) << 9) | ((13 & 0x0F) << 5) | (1 & 0x1F) # Month 13
        struct.pack_into('<H', entry_bad_date, 24, date_val)
        parsed_bad_date = self.fs._parse_directory_entry(bytes(entry_bad_date))
        self.assertIsNotNone(parsed_bad_date)
        # Check if it defaulted to 1980-01-01 or similar default
        self.assertEqual(parsed_bad_date.datetime.year, 1980)
        self.assertEqual(parsed_bad_date.datetime.month, 1)
        self.assertEqual(parsed_bad_date.datetime.day, 1)

        # Test case 3: LFN entry attribute (should be skipped)
        entry_lfn = bytearray(base_entry)
        entry_lfn[11] = 0x0F # LFN attribute
        self.assertIsNone(self.fs._parse_directory_entry(bytes(entry_lfn)))

        # Test case 4: Volume ID attribute (should be skipped)
        entry_volid = bytearray(base_entry)
        entry_volid[11] = 0x08 # Volume ID attribute
        self.assertIsNone(self.fs._parse_directory_entry(bytes(entry_volid)))

if __name__ == '__main__':
    unittest.main()
