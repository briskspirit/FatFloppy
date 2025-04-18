import unittest
from unittest.mock import MagicMock, patch, PropertyMock, call
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
FAT_SIZE_BYTES = SECTORS_PER_FAT * SECTOR_SIZE # Size of ONE FAT
FAT_AREA_SIZE_BYTES = NUM_FATS * FAT_SIZE_BYTES # Size of ALL FATs
ROOT_DIR_SIZE_BYTES = ROOT_DIR_SECTORS * SECTOR_SIZE

# Calculate offsets relative to start of disk (LBA 0)
BOOT_SECTOR_OFFSET = 0
FAT_START_OFFSET = RESERVED_SECTORS * SECTOR_SIZE
FAT_END_OFFSET = FAT_START_OFFSET + FAT_AREA_SIZE_BYTES
ROOT_DIR_START_OFFSET = FAT_END_OFFSET
ROOT_DIR_END_OFFSET = ROOT_DIR_START_OFFSET + ROOT_DIR_SIZE_BYTES
DATA_AREA_START_OFFSET = ROOT_DIR_END_OFFSET # Cluster 2 starts here

NUM_CLUSTERS = (TOTAL_SECTORS * SECTOR_SIZE - DATA_AREA_START_OFFSET) // CLUSTER_SIZE # Recalculate based on offsets

ATTR_READ_ONLY = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_VOLUME_ID = 0x08
ATTR_DIRECTORY = 0x10 # Added
ATTR_LONG_NAME = ATTR_READ_ONLY | ATTR_HIDDEN | ATTR_SYSTEM | ATTR_VOLUME_ID

class TestFATFilesystemErrors(unittest.TestCase):

    def setUp(self):
        print(f"\n--- Running test: {self.id()} ---")
        self.mock_driver = MagicMock(spec=DiskIODriver)
        self.mock_disk = MagicMock(spec=Disk)
        self.mock_disk.driver = self.mock_driver
        self.mock_disk_geometry = DiskGeometry(
            cylinders=TOTAL_SECTORS // (SECTORS_PER_TRACK * NUM_HEADS),
            heads=NUM_HEADS,
            sectors_per_track=SECTORS_PER_TRACK,
            sector_size=SECTOR_SIZE
        )
        type(self.mock_disk).geometry = PropertyMock(return_value=self.mock_disk_geometry)

        # --- Prepare Mock Data ---
        self.boot_sector_data = bytearray(SECTOR_SIZE)
        struct.pack_into('<H', self.boot_sector_data, 0x0B, SECTOR_SIZE)
        self.boot_sector_data[0x0D] = SECTORS_PER_CLUSTER
        struct.pack_into('<H', self.boot_sector_data, 0x0E, RESERVED_SECTORS)
        self.boot_sector_data[0x10] = NUM_FATS
        struct.pack_into('<H', self.boot_sector_data, 0x11, ROOT_ENTRIES)
        struct.pack_into('<H', self.boot_sector_data, 0x13, TOTAL_SECTORS)
        struct.pack_into('<I', self.boot_sector_data, 0x20, 0)
        self.boot_sector_data[0x15] = 0xF9
        struct.pack_into('<H', self.boot_sector_data, 0x16, SECTORS_PER_FAT)
        struct.pack_into('<H', self.boot_sector_data, 0x18, SECTORS_PER_TRACK)
        struct.pack_into('<H', self.boot_sector_data, 0x1A, NUM_HEADS)
        struct.pack_into('<I', self.boot_sector_data, 0x1C, 0)
        struct.pack_into('<H', self.boot_sector_data, 0x1FE, 0xAA55)

        self.fat_data = bytearray(FAT_SIZE_BYTES) # Mock data for ONE FAT copy
        self.fat_data[0:3] = bytes([0xF9, 0xFF, 0xFF]) # FAT ID bytes

        self.root_dir_data = bytearray(ROOT_DIR_SIZE_BYTES) # Mock empty root dir

        self.written_lba_data = {} # Store writes {lba: data}

        # --- Mock Disk Methods ---
        # Mock lba_to_chs directly on the instance
        def mock_lba_to_chs(lba):
            geom = self.mock_disk_geometry
            if geom.sectors_per_track == 0 or geom.heads == 0: raise ValueError("Bad geom")
            max_lba = geom.total_sectors - 1
            if not (0 <= lba <= max_lba): raise IndexError(f"LBA {lba} out of bounds (0-{max_lba})")
            sector = (lba % geom.sectors_per_track) + 1
            temp = lba // geom.sectors_per_track
            head = temp % geom.heads
            cylinder = temp // geom.heads
            # print(f"DEBUG mock_lba_to_chs({lba}) -> C:{cylinder} H:{head} S:{sector}")
            return cylinder, head, sector
        self.mock_disk.lba_to_chs = MagicMock(side_effect=mock_lba_to_chs)

        # Mock read_sectors on the mock_disk instance
        def mock_read_sectors(start_c, start_h, start_s, num_sectors):
            # print(f"DEBUG mock_read_sectors C:{start_c} H:{start_h} S:{start_s} Num:{num_sectors}")
            result = bytearray()
            geom = self.mock_disk_geometry
            try:
                # Convert start CHS to LBA
                start_lba = (start_c * geom.heads + start_h) * geom.sectors_per_track + (start_s - 1)
            except Exception as e:
                 print(f"ERROR converting CHS in mock_read_sectors: {e}")
                 return b'\x00' * num_sectors * SECTOR_SIZE # Return empty on error

            for i in range(num_sectors):
                current_lba = start_lba + i
                current_offset = current_lba * SECTOR_SIZE
                data_chunk = None # Initialize

                # --- CHECK ORDER: Written Data -> Specific Areas -> Default Zeros ---

                # 1. Check if this LBA was explicitly written during the test
                if current_lba in self.written_lba_data:
                    data_chunk = self.written_lba_data[current_lba]
                    # print(f"DEBUG mock_read_sectors: Read LBA {current_lba} from written_lba_data")

                # 2. If not written, check specific predefined areas
                elif current_lba == 0: # Boot sector LBA
                    data_chunk = self.boot_sector_data
                    # print(f"DEBUG mock_read_sectors: Read LBA {current_lba} from boot_sector_data")
                elif FAT_START_OFFSET <= current_offset < FAT_END_OFFSET: # FAT area
                    # Return from the *first* FAT copy's mock data
                    fat1_start = FAT_START_OFFSET
                    rel_offset = current_offset - fat1_start
                    # Use bounds check on self.fat_data
                    if rel_offset >= 0 and rel_offset < len(self.fat_data):
                        data_chunk = self.fat_data[rel_offset : min(rel_offset + SECTOR_SIZE, len(self.fat_data))]
                    else:
                        data_chunk = b'' # Or b'\x00' * SECTOR_SIZE if offset is valid but outside mock
                    # print(f"DEBUG mock_read_sectors: Read LBA {current_lba} from fat_data (Rel:{rel_offset})")

                elif ROOT_DIR_START_OFFSET <= current_offset < ROOT_DIR_END_OFFSET: # Root Dir area
                    rel_offset = current_offset - ROOT_DIR_START_OFFSET
                    # Use bounds check on self.root_dir_data
                    if rel_offset >= 0 and rel_offset < len(self.root_dir_data):
                         data_chunk = self.root_dir_data[rel_offset : min(rel_offset + SECTOR_SIZE, len(self.root_dir_data))]
                    else:
                         data_chunk = b'' # Or b'\x00' * SECTOR_SIZE if offset is valid but outside mock
                    # print(f"DEBUG mock_read_sectors: Read LBA {current_lba} from root_dir_data (Rel:{rel_offset})")

                # 3. If not found anywhere else, default to zeros (simulates uninitialized data area)
                else:
                    data_chunk = b'\x00' * SECTOR_SIZE
                    # print(f"DEBUG mock_read_sectors: Read LBA {current_lba} returning default zeros")

                # Ensure final chunk is exactly SECTOR_SIZE
                if len(data_chunk) < SECTOR_SIZE:
                    data_chunk += bytes(SECTOR_SIZE - len(data_chunk))
                elif len(data_chunk) > SECTOR_SIZE:
                     # This case shouldn't happen if chunks are derived correctly, but good safety check
                     data_chunk = data_chunk[:SECTOR_SIZE]

                result.extend(data_chunk)

            # print(f"DEBUG mock_read_sectors returning {len(result)} bytes")
            return bytes(result)

        # Assign the corrected mock function
        self.mock_disk.read_sector = MagicMock(side_effect=lambda c,h,s: mock_read_sectors(c,h,s,1))
        self.mock_disk.read_sectors = MagicMock(side_effect=mock_read_sectors)

        # Mock read_sector to call read_sectors with num=1
        def mock_read_sector(c, h, s):
             # print(f"DEBUG mock_read_sector C:{c} H:{h} S:{s}")
             return mock_read_sectors(c,h,s,1)
        self.mock_disk.read_sector = MagicMock(side_effect=mock_read_sector)
        self.mock_disk.read_sectors = MagicMock(side_effect=mock_read_sectors)

        # Mock write_sectors on the mock_disk instance
        def mock_write_sectors(start_c, start_h, start_s, data):
            # print(f"DEBUG mock_write_sectors C:{start_c} H:{start_h} S:{start_s} Len:{len(data)}")
            geom = self.mock_disk_geometry
            try:
                 start_lba = (start_c * geom.heads + start_h) * geom.sectors_per_track + (start_s - 1)
            except Exception as e:
                 print(f"ERROR converting CHS in mock_write_sectors: {e}")
                 return # Fail write

            num_sectors = (len(data) + SECTOR_SIZE - 1) // SECTOR_SIZE
            for i in range(num_sectors):
                current_lba = start_lba + i
                chunk_start = i * SECTOR_SIZE
                chunk_end = min(chunk_start + SECTOR_SIZE, len(data))
                chunk = data[chunk_start:chunk_end]
                if len(chunk) < SECTOR_SIZE: chunk += bytes(SECTOR_SIZE - len(chunk))
                self.written_lba_data[current_lba] = chunk # Record write by LBA

                # --- Update internal mock data stores ---
                current_offset = current_lba * SECTOR_SIZE
                # Check FAT area (update BOTH copies in mock)
                if FAT_START_OFFSET <= current_offset < FAT_END_OFFSET:
                    fat1_start = FAT_START_OFFSET
                    fat2_start = FAT_START_OFFSET + FAT_SIZE_BYTES
                    # Write to internal mock self.fat_data regardless of which copy was targeted
                    # Assumes filesystem writes to FAT1 first, then mirrors
                    if fat1_start <= current_offset < fat1_start + FAT_SIZE_BYTES:
                         rel_offset = current_offset - fat1_start
                         if rel_offset + SECTOR_SIZE <= len(self.fat_data):
                             self.fat_data[rel_offset:rel_offset+SECTOR_SIZE] = chunk
                             # print(f"DEBUG mock_write_sectors updated self.fat_data @ rel {rel_offset}")

                # Check Root Dir area
                elif ROOT_DIR_START_OFFSET <= current_offset < ROOT_DIR_END_OFFSET:
                    rel_offset = current_offset - ROOT_DIR_START_OFFSET
                    if rel_offset + SECTOR_SIZE <= len(self.root_dir_data):
                        self.root_dir_data[rel_offset:rel_offset+SECTOR_SIZE] = chunk
                        # print(f"DEBUG mock_write_sectors updated self.root_dir_data @ rel {rel_offset}")

        # Mock write_sector to call write_sectors
        def mock_write_sector(c,h,s,data):
            mock_write_sectors(c,h,s,data)
        self.mock_disk.write_sector = MagicMock(side_effect=mock_write_sector)
        self.mock_disk.write_sectors = MagicMock(side_effect=mock_write_sectors)

        # Mock flush (does nothing)
        self.mock_disk.flush = MagicMock()

        # --- Create Filesystem Instance ---
        # Now, the FS initialization will use the correctly mocked disk methods
        self.fs = FATFilesystem(self.mock_disk)

        # --- Verification ---
        # Verify FS initialization succeeded and loaded the FAT cache
        self.assertTrue(self.fs.is_valid(), "Filesystem initialization failed in setUp")
        self.assertIsNotNone(self.fs.fat_cache, "FAT cache was not loaded during FS initialization")
        # The length check is important - did it get the right size FAT?
        self.assertEqual(len(self.fs.fat_cache), FAT_SIZE_BYTES, "Loaded FAT cache has incorrect size")
        self.assertFalse(self.fs.fat_dirty, "FAT cache should be clean after initialization")
        # Check if the FAT ID bytes were loaded correctly
        self.assertEqual(self.fs.fat_cache[:3], bytes([0xF9, 0xFF, 0xFF]), "FAT ID mismatch in loaded cache")


    def tearDown(self):
        print(f"--- Finished test: {self.id()} ---") # Simple teardown message

    # --- Test Cases ---

    def test_01_write_file_invalid_name(self):
        """Test write_file fails with invalid 8.3 filename"""
        # ... (no changes needed here) ...
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
        # ... (no changes needed here) ...
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

        # --- Mock Setup ---
        # 1. Mock _list_directory_by_cluster specifically for this test
        #    to indicate non-emptiness for the target directory cluster.
        mock_file_entry = FileInfo(name="dummy.txt", size=10, is_dir=False, datetime=datetime.datetime.now(), attributes="-A-", starting_cluster=6)
        original_list_cluster = self.fs._list_directory_by_cluster
        def mock_list_cluster_for_test(cluster):
            if cluster == dir_cluster:
                # print(f"DEBUG mock_list_cluster_for_test: Returning non-empty list for cluster {cluster}")
                return [mock_file_entry]
            else:
                # print(f"DEBUG mock_list_cluster_for_test: Calling original for cluster {cluster}")
                # For other clusters (like root), rely on the setUp mocks reading the data
                return original_list_cluster(cluster)
        patch_list_cluster = patch.object(self.fs, '_list_directory_by_cluster', side_effect=mock_list_cluster_for_test)
        mock_list_cluster_func = patch_list_cluster.start()
        self.addCleanup(patch_list_cluster.stop)

        # 2. Create the directory entry in the *mock root directory data*
        mock_dir_entry_info = FileInfo(name=dir_name_to_delete, size=0, is_dir=True, datetime=datetime.datetime.now(), attributes="D", starting_cluster=dir_cluster)
        deldir_entry_bytes = self.fs._create_directory_entry_bytes(
            name=dir_name_to_delete,
            is_dir=True,
            starting_cluster=dir_cluster,
            size=0,
            dt=mock_dir_entry_info.datetime
        )
        # Place it at the beginning of the mock root dir buffer
        self.root_dir_data[:32] = deldir_entry_bytes
        # print(f"DEBUG DeleteTest: Added DELDIR entry to mock root_dir_data: {self.root_dir_data[:64].hex(' ')}")

        # --- Execute Test ---
        with self.assertRaisesRegex(OSError, "Directory not empty", msg="Delete should fail because directory is not empty"):
            self.fs.delete(dir_path_to_delete)

        # --- Assertions ---
        # Verify _list_directory_by_cluster was called to check emptiness for the target dir
        mock_list_cluster_func.assert_any_call(dir_cluster)
        # Verify the directory entry in the root dir was NOT marked deleted (byte[0] != 0xE5)
        # Read the root dir data back via the mock
        root_c, root_h, root_s = self.mock_disk.lba_to_chs(ROOT_DIR_START_OFFSET // SECTOR_SIZE)
        first_root_sector = self.mock_disk.read_sector(root_c, root_h, root_s)
        self.assertNotEqual(first_root_sector[0], 0xE5, "Directory entry should not have been marked deleted")


    def test_09_write_no_free_clusters(self):
        """Test write_file fails when no free clusters are available"""
        # Mock find_free_cluster to return None *after* the filesystem is initialized
        with patch.object(self.fs, '_find_free_cluster', return_value=None) as mock_find_free:
             with self.assertRaisesRegex((IOError, OSError), "Not enough free space"): # More flexible exception check
                 self.fs.write_file("/TEST.TXT", b"some data")
             mock_find_free.assert_called() # Ensure it was actually called


    def test_10_write_no_free_dir_entry_root(self):
        """Test write_file fails when root directory is full"""
        # Make the mock root directory appear full (no 0x00 or 0xE5)
        # Fill with valid-looking but dummy entries
        dummy_entry = self.fs._create_directory_entry_bytes("FILLER.   ", False, 0, 0, datetime.datetime.now())
        for i in range(0, len(self.root_dir_data), 32):
            self.root_dir_data[i:i+32] = dummy_entry

        # Mock cluster allocation to succeed initially
        with patch.object(self.fs, '_allocate_cluster_chain', return_value=[5]) as mock_alloc:
             with patch.object(self.fs, '_free_cluster_chain') as mock_free:
                 with self.assertRaisesRegex((IOError, OSError), "No space in directory /"):
                     self.fs.write_file("/TEST.TXT", b"data")
                 mock_alloc.assert_called_once_with(1) # Assuming "data" fits in 1 cluster
                 mock_free.assert_called_once_with(5) # Ensure cleanup happened


    def test_11_write_no_free_dir_entry_subdir(self):
        """Test write_file fails when subdirectory cannot be extended"""
        subdir_name = "SUB"
        subdir_path = f"/{subdir_name}"
        subdir_cluster = 5

        # 1. Create SUBDIR entry in the mock root directory data
        subdir_entry_bytes = self.fs._create_directory_entry_bytes(
            subdir_name, True, subdir_cluster, 0, datetime.datetime.now()
        )
        self.root_dir_data[0:32] = subdir_entry_bytes

        # 2. Create mock data for the subdirectory cluster, make it appear full
        subdir_data_offset = self.fs._cluster_to_offset(subdir_cluster)
        subdir_lba = subdir_data_offset // SECTOR_SIZE
        subdir_lba_end = (subdir_data_offset + self.fs.cluster_size - 1) // SECTOR_SIZE
        dummy_entry = self.fs._create_directory_entry_bytes("FILLER.   ", False, 0, 0, datetime.datetime.now())

        # Update the mock read_sectors to return this 'full' subdir data
        original_read_sectors = self.mock_disk.read_sectors.side_effect
        def read_sectors_for_subdir_test(start_c, start_h, start_s, num_sectors):
             geom = self.mock_disk_geometry
             start_lba = (start_c * geom.heads + start_h) * geom.sectors_per_track + (start_s - 1)
             # Check if reading the subdir cluster
             if start_lba <= subdir_lba <= start_lba + num_sectors -1:
                  # Return 'full' data for the subdir cluster(s)
                  print(f"DEBUG read_sectors_for_subdir_test: Intercepting read for SUBDIR cluster {subdir_cluster} (LBA {subdir_lba})")
                  full_subdir_cluster_data = dummy_entry * (self.fs.cluster_size // 32)
                  # Construct the result, combining original data and mocked subdir data
                  result = bytearray()
                  for i in range(num_sectors):
                      current_lba = start_lba + i
                      if subdir_lba <= current_lba <= subdir_lba_end:
                           offset_in_full = (current_lba - subdir_lba) * SECTOR_SIZE
                           result.extend(full_subdir_cluster_data[offset_in_full:offset_in_full+SECTOR_SIZE])
                      else:
                           # Read normally using original mock for other sectors
                           c, h, s = self.mock_disk.lba_to_chs(current_lba)
                           result.extend(original_read_sectors(c,h,s,1))
                  return bytes(result)
             else:
                 # Fallback to original mock
                 return original_read_sectors(start_c, start_h, start_s, num_sectors)
        self.mock_disk.read_sectors.side_effect = read_sectors_for_subdir_test
        self.mock_disk.read_sector.side_effect = lambda c,h,s: read_sectors_for_subdir_test(c,h,s,1)


        # 3. Mock _find_free_cluster so extension attempt fails
        with patch.object(self.fs, '_find_free_cluster', return_value=None) as mock_find_free:
             # Mock cluster allocation for file data to succeed
             with patch.object(self.fs, '_allocate_cluster_chain', return_value=[6]) as mock_alloc:
                 with patch.object(self.fs, '_free_cluster_chain') as mock_free:
                      with self.assertRaisesRegex((IOError, OSError), f"No space in directory {subdir_path}"):
                          self.fs.write_file(f"{subdir_path}/TEST.TXT", b"data")
                      mock_alloc.assert_called_once_with(1)
                      mock_free.assert_called_once_with(6)
                      # Check that find_free_cluster was called (during the attempt to extend the dir)
                      mock_find_free.assert_called()

        # Restore original mock after test
        self.mock_disk.read_sectors.side_effect = original_read_sectors
        self.mock_disk.read_sector.side_effect = lambda c,h,s: original_read_sectors(c,h,s,1)


    def test_12_create_dir_no_free_clusters(self):
        """Test create_directory fails when no free clusters are available"""
        # Mock _find_free_cluster to return None
        with patch.object(self.fs, '_find_free_cluster', return_value=None) as mock_find_free:
             with self.assertRaisesRegex((IOError, OSError), "No free clusters available"):
                 self.fs.create_directory("/NEWDIR")
             mock_find_free.assert_called()


    def test_13_create_dir_no_free_entry(self):
        """Test create_directory fails when parent directory is full"""
        # Fill the mock root directory data
        dummy_entry = self.fs._create_directory_entry_bytes("FILLER.   ", False, 0, 0, datetime.datetime.now())
        for i in range(0, len(self.root_dir_data), 32):
            self.root_dir_data[i:i+32] = dummy_entry

        # Mock cluster allocation for the new dir's content to succeed
        with patch.object(self.fs, '_find_free_cluster', return_value=5) as mock_find_cluster:
            # Mock the FAT write for freeing the cluster during cleanup
            with patch.object(self.fs, '_set_fat_entry_cached') as mock_set_fat:
                with self.assertRaisesRegex((IOError, OSError), "No space in parent directory /"):
                     self.fs.create_directory("/NEWDIR")
                # Check cluster 5 was allocated
                mock_find_cluster.assert_called()
                # Check cluster 5 was freed (set to 0)
                mock_set_fat.assert_any_call(5, 0)


    def test_14_fat12_odd_cluster_read_write(self):
        """Test reading/writing FAT entry for an odd cluster number"""
        cluster = 3 # Odd cluster
        byte_offset = int(cluster * 1.5) # 3 * 1.5 = 4.5 -> 4

        # Simulate existing value in the 2 bytes at offset 4 in the *mock* FAT data
        initial_word = 0xDEF3
        struct.pack_into('<H', self.fat_data, byte_offset, initial_word)
        # Make sure the filesystem's internal cache reflects this *initial* state
        self.fs.fat_cache = self.fat_data[:] # Make a copy

        # --- Test Write ---
        value_to_write = 0xABC
        expected_word = (initial_word & 0x000F) | (value_to_write << 4) # 0xABC3

        self.fs._set_fat_entry_cached(cluster, value_to_write)

        # Verify internal fat_cache
        written_word = struct.unpack_from('<H', self.fs.fat_cache, byte_offset)[0]
        self.assertEqual(written_word, expected_word, "FAT cache write incorrect for odd cluster")
        self.assertTrue(self.fs.fat_dirty, "FAT cache should be dirty after write")

        # --- Test Read ---
        # Reset dirty flag and set cache for read test
        self.fs.fat_dirty = False
        self.fs.fat_cache = bytearray(FAT_SIZE_BYTES) # Clear cache
        struct.pack_into('<H', self.fs.fat_cache, byte_offset, expected_word) # Set word to read

        read_value = self.fs._read_fat_entry_cached(cluster)
        self.assertEqual(read_value, value_to_write, "FAT cache read incorrect for odd cluster")
        self.assertFalse(self.fs.fat_dirty, "FAT cache should not be dirty after read")


    def test_15_fat12_even_cluster_read_write(self):
        """Test reading/writing FAT entry for an even cluster number"""
        cluster = 2 # Even cluster
        byte_offset = int(cluster * 1.5) # 2 * 1.5 = 3

        # Simulate existing value
        initial_word = 0xDE23
        struct.pack_into('<H', self.fat_data, byte_offset, initial_word)
        self.fs.fat_cache = self.fat_data[:]

        # --- Test Write ---
        value_to_write = 0x321
        expected_word = (initial_word & 0xF000) | (value_to_write & 0x0FFF) # 0xD321

        self.fs._set_fat_entry_cached(cluster, value_to_write)

        written_word = struct.unpack_from('<H', self.fs.fat_cache, byte_offset)[0]
        self.assertEqual(written_word, expected_word, "FAT cache write incorrect for even cluster")
        self.assertTrue(self.fs.fat_dirty, "FAT cache should be dirty after write")

        # --- Test Read ---
        self.fs.fat_dirty = False
        self.fs.fat_cache = bytearray(FAT_SIZE_BYTES)
        struct.pack_into('<H', self.fs.fat_cache, byte_offset, expected_word)

        read_value = self.fs._read_fat_entry_cached(cluster)
        self.assertEqual(read_value, value_to_write, "FAT cache read incorrect for even cluster")
        self.assertFalse(self.fs.fat_dirty, "FAT cache should not be dirty after read")


    def test_16_find_free_cluster(self):
        """Test _find_free_cluster logic"""
        # Ensure filesystem's cache reflects test state
        self.fs.fat_cache = self.fat_data[:]

        # Simulate clusters 2, 3 used, 4 free
        self.fs._set_fat_entry_cached(2, 0xFFF) # Used
        self.fs._set_fat_entry_cached(3, 0xFFF) # Used
        self.fs._set_fat_entry_cached(4, 0)     # Free
        # Clusters >= 5 are implicitly free (0) in the initial self.fat_data

        free_cluster = self.fs._find_free_cluster()
        self.assertEqual(free_cluster, 4)

        # Mark 4 as used and check again
        self.fs._set_fat_entry_cached(4, 0xFFF)
        free_cluster = self.fs._find_free_cluster()
        self.assertEqual(free_cluster, 5) # Next available


    def test_17_find_free_cluster_none_free(self):
        """Test _find_free_cluster when disk is full"""
        self.fs.fat_cache = self.fat_data[:]

        # Mark all clusters as used
        for i in range(2, self.fs.num_clusters + 2):
             self.fs._set_fat_entry_cached(i, 0xFFF) # Use EOC marker

        free_cluster = self.fs._find_free_cluster()
        self.assertIsNone(free_cluster)


    def test_18_fs_multi_sector_read_write_edge(self):
        """Test _read_bytes and _write_bytes across sector boundaries"""
        sector_size = self.fs.boot_sector.bytes_per_sector
        # Choose offset/length carefully based on calculated data area start
        base_offset = DATA_AREA_START_OFFSET # Start of cluster 2
        test_offset = base_offset + sector_size - 50 # Start 50 bytes before end of first data sector
        test_len = 150 # Read/Write 150 bytes
        test_data = bytes([i % 256 for i in range(test_len)])

        # Determine the LBAs involved
        start_lba = test_offset // sector_size
        end_lba = (test_offset + test_len - 1) // sector_size
        # print(f"DEBUG test_18: test_offset={test_offset}, test_len={test_len}, start_lba={start_lba}, end_lba={end_lba}")

        # Use the setUp mocks for read/write_sectors

        # Write data using the filesystem's internal _write_bytes
        self.fs._write_bytes(test_offset, test_data)

        # Verify write_sectors was called via _write_bytes logic using the LBA store
        self.assertIn(start_lba, self.written_lba_data, f"LBA {start_lba} not written")
        # Check data within the first LBA written
        bytes_in_first_lba = sector_size - (test_offset % sector_size)
        offset_in_lba = test_offset % sector_size
        self.assertEqual(self.written_lba_data[start_lba][offset_in_lba:], test_data[:bytes_in_first_lba], "Data mismatch in first LBA")

        if start_lba != end_lba: # Check second sector if span occurred
            self.assertIn(end_lba, self.written_lba_data, f"LBA {end_lba} not written")
            bytes_in_second_lba = test_len - bytes_in_first_lba
            self.assertEqual(self.written_lba_data[end_lba][:bytes_in_second_lba], test_data[bytes_in_first_lba:], "Data mismatch in second LBA")

        # Read back the data using _read_bytes, which should use the mock read_sectors
        read_back_data = self.fs._read_bytes(test_offset, test_len)
        self.assertEqual(read_back_data, test_data, "Read back data does not match original test data")


    def test_19_fs_parse_corrupt_entry(self):
        """Test parsing specifically crafted corrupt directory entries"""
        base_entry = self.fs._create_directory_entry_bytes("GOODFILE.TXT", False, 5, 100, datetime.datetime.now())

        # Test case 1: Invalid characters in name
        entry_bad_char = bytearray(base_entry)
        entry_bad_char[0] = ord('*') # Invalid char
        # This should now be skipped during parsing
        self.assertIsNone(self.fs._parse_single_directory_entry(bytes(entry_bad_char)), "Entry with bad char should be skipped")

        # Test case 2: Invalid date/time (e.g., month 13) - should default
        entry_bad_date = bytearray(base_entry)
        # date_val format: YYYYYYYMMMMDDDDD
        year_part = (2020 - 1980) << 9
        month_part = 13 << 5 # Invalid month
        day_part = 1
        date_val = year_part | month_part | day_part
        struct.pack_into('<H', entry_bad_date, 24, date_val)
        parsed_bad_date = self.fs._parse_single_directory_entry(bytes(entry_bad_date))
        self.assertIsNotNone(parsed_bad_date, "Entry with bad date should still parse")
        self.assertEqual(parsed_bad_date.datetime.year, 1980, "Bad date year should default")
        self.assertEqual(parsed_bad_date.datetime.month, 1, "Bad date month should default")
        self.assertEqual(parsed_bad_date.datetime.day, 1, "Bad date day should default")

        # Test case 3: LFN entry attribute (should be skipped)
        entry_lfn = bytearray(base_entry)
        entry_lfn[11] = ATTR_LONG_NAME # Use constant
        self.assertIsNone(self.fs._parse_single_directory_entry(bytes(entry_lfn)), "LFN entry should be skipped")

        # Test case 4: Volume ID attribute (should be parsed with VOL attribute)
        entry_volid = bytearray(base_entry)
        vol_label_padded = b'VOL LABEL'.ljust(11) # Pad with spaces
        entry_volid[0:11] = vol_label_padded      # Set all 11 bytes
        entry_volid[11] = ATTR_VOLUME_ID # Use constant
        parsed_vol = self.fs._parse_single_directory_entry(bytes(entry_volid))
        self.assertIsNotNone(parsed_vol, "Volume ID entry should parse")
        self.assertEqual(parsed_vol.name, "VOL LABEL", "Volume label name mismatch")
        self.assertEqual(parsed_vol.attributes, "VOL", "Volume label attribute mismatch")


if __name__ == '__main__':
    unittest.main()
