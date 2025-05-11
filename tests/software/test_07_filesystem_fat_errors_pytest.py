# tests/software/test_07_filesystem_fat_errors_pytest.py
import pytest
import sys
import datetime
import struct
from unittest.mock import MagicMock, patch, PropertyMock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.filesystems.fat12fs import FATFilesystem, FATVolumeInfo, FileInfo
from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers import DiskIODriver
from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat

# Constants defined for clarity in the fixture
bytes_per_sector = 512
SECTORS_PER_CLUSTER = 2
RESERVED_SECTORS = 1
NUM_FATS = 2
ROOT_ENTRIES = 112
TOTAL_SECTORS = 1440 # Typically 720k
SECTORS_PER_FAT = 3
SECTORS_PER_TRACK = 9
NUM_HEADS = 2
allocation_unit_size = bytes_per_sector * SECTORS_PER_CLUSTER
ROOT_DIR_SECTORS = (ROOT_ENTRIES * 32 + bytes_per_sector - 1) // bytes_per_sector
FAT_SIZE_BYTES = SECTORS_PER_FAT * bytes_per_sector
FAT_AREA_SIZE_BYTES = NUM_FATS * FAT_SIZE_BYTES
ROOT_DIR_SIZE_BYTES = ROOT_DIR_SECTORS * bytes_per_sector
BOOT_SECTOR_OFFSET = 0
FAT_START_OFFSET = RESERVED_SECTORS * bytes_per_sector
FAT_END_OFFSET = FAT_START_OFFSET + FAT_AREA_SIZE_BYTES
ROOT_DIR_START_OFFSET = FAT_END_OFFSET
ROOT_DIR_END_OFFSET = ROOT_DIR_START_OFFSET + ROOT_DIR_SIZE_BYTES
DATA_AREA_START_OFFSET = ROOT_DIR_END_OFFSET
# Careful calculation for num_clusters based on total size and offsets
TOTAL_BYTES = TOTAL_SECTORS * bytes_per_sector
DATA_AREA_BYTES = TOTAL_BYTES - DATA_AREA_START_OFFSET
NUM_CLUSTERS = DATA_AREA_BYTES // allocation_unit_size if allocation_unit_size > 0 else 0

# Derived constants for geometry
CYLINDERS = TOTAL_SECTORS // (SECTORS_PER_TRACK * NUM_HEADS)
RPM = 300 # Standard for 720k/1.44M
RATE = 250 # Standard for 720k DD
ENCODING = "MFM"
GAP3 = 84 # Common default
INTERLEAVE = 1

ATTR_READ_ONLY = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_VOLUME_ID = 0x08
ATTR_DIRECTORY = 0x10
ATTR_ARCHIVE = 0x20
ATTR_LONG_NAME = ATTR_READ_ONLY | ATTR_HIDDEN | ATTR_SYSTEM | ATTR_VOLUME_ID


@pytest.fixture(scope="function")
def mock_fs_setup(request):
    print(f"\n--- [Fixture Setup] Mocking FS for test: {request.node.name} ---")
    mock_driver = MagicMock(spec=DiskIODriver)
    mock_disk = MagicMock(spec=Disk)
    mock_disk.driver = mock_driver

    # --- FIX: Define PhysicalFormat correctly with TrackFormat ---
    mock_track_format = TrackFormat(
        track_start=0,
        track_end=CYLINDERS - 1,
        head_start=0,
        head_end=NUM_HEADS - 1,
        sectors_per_track=SECTORS_PER_TRACK,
        encoding=ENCODING,
        rate=RATE,
        gap3=GAP3,
        interleave=INTERLEAVE
    )
    mock_disk_geometry = PhysicalFormat(
        cylinders=CYLINDERS,
        heads=NUM_HEADS,
        rpm=RPM,
        heads_inverted=False,
        bytes_per_sector=bytes_per_sector,
        track_formats=[mock_track_format] # Pass list of TrackFormat
    )
    # Use PropertyMock for geometry as it might be accessed multiple times
    type(mock_disk).physical_format = PropertyMock(return_value=mock_disk_geometry)
    # --- End Fix ---

    # Create initial in-memory representations
    boot_sector_data = bytearray(bytes_per_sector)
    struct.pack_into("<H", boot_sector_data, 0x0B, bytes_per_sector)
    boot_sector_data[0x0D] = SECTORS_PER_CLUSTER
    struct.pack_into("<H", boot_sector_data, 0x0E, RESERVED_SECTORS)
    boot_sector_data[0x10] = NUM_FATS
    struct.pack_into("<H", boot_sector_data, 0x11, ROOT_ENTRIES)
    struct.pack_into("<H", boot_sector_data, 0x13, TOTAL_SECTORS) # Total sectors 16
    struct.pack_into("<I", boot_sector_data, 0x20, 0) # Total sectors 32 (0 means use 16-bit value)
    boot_sector_data[0x15] = 0xF9 # Media descriptor for 720k
    struct.pack_into("<H", boot_sector_data, 0x16, SECTORS_PER_FAT)
    struct.pack_into("<H", boot_sector_data, 0x18, SECTORS_PER_TRACK)
    struct.pack_into("<H", boot_sector_data, 0x1A, NUM_HEADS)
    struct.pack_into("<I", boot_sector_data, 0x1C, 0) # Hidden sectors
    struct.pack_into("<H", boot_sector_data, 0x1FE, 0xAA55) # Boot signature

    fat_data = bytearray(FAT_SIZE_BYTES)
    fat_data[0:3] = bytes([0xF9, 0xFF, 0xFF]) # FAT ID and reserved entries

    root_dir_data = bytearray(ROOT_DIR_SIZE_BYTES) # Initially empty

    written_lba_data = {} # To store data written by mock_write_sectors

    # Mock LBA to CHS conversion
    def mock_lba_to_chs(lba):
        geom = mock_disk_geometry # Use the defined geometry
        # Access geometry via the object method now
        spt = geom.get_sectors_per_track(0, 0) # Assume uniform for mock
        heads = geom.heads
        if spt <= 0 or heads <= 0:
            raise ValueError("Bad geom in mock_lba_to_chs")
        max_lba = geom.total_sectors - 1
        if not 0 <= lba <= max_lba:
            # Raising IndexError is more accurate than ValueError here
            raise IndexError(f"LBA {lba} out of bounds (0-{max_lba})")
        sector = (lba % spt) + 1
        temp = lba // spt
        head = temp % heads
        cylinder = temp // heads
        return cylinder, head, sector

    mock_disk.lba_to_chs.side_effect = mock_lba_to_chs

    # Mock reading sectors from in-memory data
    def mock_read_sectors(start_c, start_h, start_s, num_sectors):
        result = bytearray()
        geom = mock_disk_geometry
        spt = geom.get_sectors_per_track(start_c, start_h)
        heads = geom.heads
        bps = geom.bytes_per_sector
        try:
            # Calculate starting LBA from CHS input
            start_lba = (
                (start_c * heads + start_h) * spt
                + (start_s - 1)
            )
        except Exception as e:
             # Handle potential calculation errors gracefully
            print(f"DEBUG: Error calculating start LBA in mock_read_sectors: C={start_c} H={start_h} S={start_s}, Error: {e}")
            return b"\x00" * num_sectors * bps # Return zeroed data on error

        # Iterate through the requested number of sectors
        for i in range(num_sectors):
            current_lba = start_lba + i
            # Check bounds
            if not 0 <= current_lba < geom.total_sectors:
                 print(f"DEBUG: mock_read_sectors attempted read beyond disk bounds: LBA {current_lba} (Max LBA: {geom.total_sectors - 1})")
                 data_chunk = b"\x00" * bps # Return zeroed data for out-of-bounds reads
            else:
                # Calculate the byte offset for this LBA
                current_offset = current_lba * bps

                # Prioritize recently written data
                if current_lba in written_lba_data:
                    data_chunk = written_lba_data[current_lba]
                # Check if reading the boot sector
                elif current_lba == 0:
                    data_chunk = boot_sector_data
                # Check if reading from the FAT area
                elif FAT_START_OFFSET <= current_offset < FAT_END_OFFSET:
                    rel_offset = current_offset - FAT_START_OFFSET
                    # Ensure reading within the bounds of fat_data
                    data_chunk = (
                        fat_data[rel_offset : rel_offset + bps]
                        if 0 <= rel_offset < len(fat_data)
                        else b"\x00" * bps # Pad if request goes beyond fat_data
                    )
                # Check if reading from the root directory area
                elif ROOT_DIR_START_OFFSET <= current_offset < ROOT_DIR_END_OFFSET:
                    rel_offset = current_offset - ROOT_DIR_START_OFFSET
                    # Ensure reading within the bounds of root_dir_data
                    data_chunk = (
                        root_dir_data[rel_offset : rel_offset + bps]
                        if 0 <= rel_offset < len(root_dir_data)
                        else b"\x00" * bps # Pad if request goes beyond root_dir_data
                    )
                # Otherwise, it's the data area (or unused space), return zeros
                else:
                    data_chunk = b"\x00" * bps

            # Ensure the chunk is exactly the sector size
            data_chunk = data_chunk.ljust(bps, b"\x00")
            result.extend(data_chunk[:bps]) # Append the correctly sized chunk

        return bytes(result) # Return the combined data as bytes

    # Configure the mock disk object's methods
    mock_disk.read_sector.side_effect = lambda c, h, s: mock_read_sectors(c, h, s, 1)
    mock_disk.read_sectors.side_effect = mock_read_sectors

    # FIX: Configure mock_disk.read_boot_sector
    mock_disk.read_boot_sector.return_value = bytes(boot_sector_data)

    # Mock writing sectors to in-memory data and written_lba_data cache
    def mock_write_sectors(start_c, start_h, start_s, data):
        geom = mock_disk_geometry
        spt = geom.get_sectors_per_track(start_c, start_h)
        heads = geom.heads
        bps = geom.bytes_per_sector
        try:
             # Calculate starting LBA from CHS input
            start_lba = (
                (start_c * heads + start_h) * spt
                + (start_s - 1)
            )
        except Exception as e:
            print(f"DEBUG: Error calculating start LBA in mock_write_sectors: C={start_c} H={start_h} S={start_s}, Error: {e}")
            return # Abort write on calculation error

        num_sectors = (len(data) + bps - 1) // bps

        # Iterate through the sectors to be written
        for i in range(num_sectors):
            current_lba = start_lba + i
            # Check bounds
            if not 0 <= current_lba < geom.total_sectors:
                print(f"DEBUG: mock_write_sectors attempted write beyond disk bounds: LBA {current_lba} (Max LBA: {geom.total_sectors - 1})")
                continue # Skip writing out-of-bounds sectors

            # Extract the data chunk for the current sector
            chunk_start = i * bps
            chunk_end = min(chunk_start + bps, len(data))
            chunk = data[chunk_start:chunk_end].ljust(bps, b"\x00")

            # Store the written data in the cache
            written_lba_data[current_lba] = chunk

            # Also update the in-memory representations for FAT and Root Dir if applicable
            current_offset = current_lba * bps
            # Update FAT area (both copies conceptually, though fat_data only holds one)
            if FAT_START_OFFSET <= current_offset < FAT_END_OFFSET:
                # Update first FAT copy
                fat1_start = FAT_START_OFFSET
                rel_offset1 = current_offset - fat1_start
                if rel_offset1 >= 0 and rel_offset1 + bps <= len(fat_data):
                    fat_data[rel_offset1 : rel_offset1 + bps] = chunk
                # Update second FAT copy (if it exists based on NUM_FATS > 1)
                if NUM_FATS > 1:
                     fat2_start = FAT_START_OFFSET + FAT_SIZE_BYTES
                     if fat2_start <= current_offset < fat2_start + FAT_SIZE_BYTES:
                          rel_offset2 = current_offset - fat2_start
                          # This assumes fat_data holds only the first copy,
                          # but the write affects the 'disk' which would have both.
                          # The mock needs to handle this if mirror consistency is tested directly.
                          # For now, we just update the first copy in our mock state.
                          pass # No separate fat2_data in this simple mock


            # Update Root Directory area
            elif ROOT_DIR_START_OFFSET <= current_offset < ROOT_DIR_END_OFFSET:
                rel_offset = current_offset - ROOT_DIR_START_OFFSET
                if rel_offset >= 0 and rel_offset + bps <= len(root_dir_data):
                    root_dir_data[rel_offset : rel_offset + bps] = chunk

    # Configure the mock disk object's write methods
    mock_disk.write_sector.side_effect = lambda c, h, s, data: mock_write_sectors(c, h, s, data)
    mock_disk.write_sectors.side_effect = mock_write_sectors
    mock_disk.flush.return_value = None # Mock flush does nothing

    # Now initialize the filesystem, which will call the mocked read_boot_sector
    fs = FATFilesystem(mock_disk)

    # Check if initialization worked (it should now)
    if not fs.is_valid():
         pytest.fail("FATFilesystem failed to initialize in mock_fs_setup fixture after fixing read_boot_sector mock.")

    # Store the mutable mock state for tests to access/modify if needed (use with caution)
    mock_state = {
        "boot_sector_data": boot_sector_data,
        "fat_data": fat_data,
        "root_dir_data": root_dir_data,
        "written_lba_data": written_lba_data,
    }
    yield fs, mock_state
    print(f"--- [Fixture Teardown] Mock FS test: {request.node.name} ---")


# =========================================
# Test Functions (No changes needed below)
# =========================================

def test_01_write_file_invalid_name(mock_fs_setup):
    fs, _ = mock_fs_setup
    invalid_names = [
        "BAD NAME.TXT", "TOOLONGNAM.TXT", "FILE.TOOLONGEXT",
        "COM1", ".BADNAME", "GOOD.", "INVALID*CHAR",
    ]
    for name in invalid_names:
        with pytest.raises(ValueError, match="Invalid 8.3 filename"):
            fs.write_file(name, b"data")

def test_02_create_dir_invalid_name(mock_fs_setup):
    fs, _ = mock_fs_setup
    invalid_names = [
        "BAD NAME", "TOOLONGDIRNAME", "DIR.EXT",
        "COM1", ".BADDIR", "GOODDIR.", "INVALID?CHAR",
    ]
    for name in invalid_names:
        with pytest.raises(ValueError, match="Invalid 8.3 directory name"):
            fs.create_directory(name)

def test_03_delete_non_empty_directory(mock_fs_setup):
    fs, mock_state = mock_fs_setup
    dir_name = "DELDIR"
    dir_path = f"/{dir_name}"
    dir_cluster = 5
    # Create the directory entry in the root directory mock data
    deldir_entry_bytes = fs._create_directory_entry_bytes(
        dir_name, True, dir_cluster, 0, datetime.datetime.now()
    )
    mock_state["root_dir_data"][:32] = deldir_entry_bytes
    # Simulate the directory cluster being allocated in FAT
    fs._set_fat_entry_cached(dir_cluster, 0xFFF)
    fs._commit_fat() # Ensure FAT write is simulated
    fs.fat_cache = None # Clear cache to force reload if needed

    # Mock _list_directory_by_cluster to simulate the directory being non-empty
    mock_file_entry = FileInfo(
        name="dummy.txt", size=10, is_dir=False,
        datetime=datetime.datetime.now(), attributes="-A-", starting_cluster=6
    )
    original_list_cluster = fs._list_directory_by_cluster
    def mock_list_cluster_side_effect(cluster):
        if cluster == dir_cluster:
            print(f"DEBUG: Mocking _list_directory_by_cluster for cluster {cluster} -> returning non-empty list")
            return [mock_file_entry]
        else:
            # Call the original method for other clusters (like root)
            # Need to reload filesystem state potentially if cache was cleared
            if fs.fat_cache is None: fs._load_fat_cache()
            return original_list_cluster(cluster)

    with patch.object(fs, "_list_directory_by_cluster", side_effect=mock_list_cluster_side_effect) as mock_list_func:
        with pytest.raises(OSError, match=f"Could not verify directory contents before deleting: {dir_path}"):
            fs.delete(dir_path)
        # Verify that the method was called for the target directory cluster
        mock_list_func.assert_any_call(dir_cluster)
        # Check that the root directory entry was NOT marked as deleted
        reloaded_root_data = fs._read_bytes(ROOT_DIR_START_OFFSET, 32)
        assert reloaded_root_data[0] != 0xE5

def test_09_write_no_free_clusters(mock_fs_setup):
    fs, _ = mock_fs_setup
    # FIX: Patch the method that finds free clusters
    with patch.object(fs, "_find_free_cluster", return_value=None) as mock_find_free:
        with pytest.raises(IOError, match="Not enough free space"): # Match the actual expected exception
            fs.write_file("/TEST.TXT", b"some data")
        mock_find_free.assert_called() # Verify the patched method was called

def test_10_write_no_free_dir_entry_root(mock_fs_setup):
    fs, _ = mock_fs_setup
     # FIX: Patch the method that finds free directory entries
    with patch.object(fs, "_find_free_directory_entry", return_value=None) as mock_find_entry:
        # Mock allocation to simulate that part succeeding before entry finding fails
        with patch.object(fs, "_allocate_cluster_chain", return_value=[5]) as mock_alloc:
             with patch.object(fs, "_free_cluster_chain") as mock_free:
                with pytest.raises(IOError, match="No space in directory /"): # Match the actual expected exception
                     fs.write_file("/TEST.TXT", b"data")
                mock_find_entry.assert_called_once_with(0) # Check it was called for root (cluster 0)
                mock_alloc.assert_called_once_with(1)
                mock_free.assert_called_once_with(5) # Ensure cleanup allocation was freed

def test_11_write_no_free_dir_entry_subdir(mock_fs_setup):
    fs, mock_state = mock_fs_setup
    subdir_name = "SUB"
    subdir_path = f"/{subdir_name}"
    subdir_cluster = 5 # Assume this cluster is allocated for the dir
    # Minimal setup: create the subdir entry in root, assume cluster 5 is allocated (FFF in FAT)
    subdir_entry_bytes = fs._create_directory_entry_bytes(
        subdir_name, True, subdir_cluster, 0, datetime.datetime.now()
    )
    mock_state["root_dir_data"][0:32] = subdir_entry_bytes
    fs._set_fat_entry_cached(subdir_cluster, 0xFFF) # Mark cluster as used for dir
    fs.fat_dirty = True # Ensure commit happens if needed
    fs._commit_fat()
    fs.fat_cache = None # Clear cache

    # FIX: Patch the method that finds free directory entries
    with patch.object(fs, "_find_free_directory_entry", return_value=None) as mock_find_entry:
         # Mock allocation to simulate that part succeeding
        with patch.object(fs, "_allocate_cluster_chain", return_value=[6]) as mock_alloc:
             with patch.object(fs, "_free_cluster_chain") as mock_free:
                with pytest.raises(IOError, match=f"No space in directory {subdir_path}"): # Match actual exception
                    fs.write_file(f"{subdir_path}/TEST.TXT", b"data")
                # Check it was called for the subdir cluster
                mock_find_entry.assert_any_call(subdir_cluster)
                mock_alloc.assert_called_once_with(1)
                mock_free.assert_called_once_with(6)

def test_12_create_dir_no_free_clusters(mock_fs_setup):
    fs, _ = mock_fs_setup
    # FIX: Patch the method that finds free clusters
    with patch.object(fs, "_find_free_cluster", return_value=None) as mock_find_free:
        with pytest.raises(IOError, match="No free clusters available"): # Match actual exception
            fs.create_directory("/NEWDIR")
        mock_find_free.assert_called()

def test_13_create_dir_no_free_entry(mock_fs_setup):
    fs, _ = mock_fs_setup
    # FIX: Patch the method that finds free directory entries
    with patch.object(fs, "_find_free_directory_entry", return_value=None) as mock_find_entry:
         # Mock cluster finding to simulate that part succeeding
        with patch.object(fs, "_find_free_cluster", return_value=5) as mock_find_cluster:
             # Mock FAT setting to check if cleanup happens
            with patch.object(fs, "_set_fat_entry_cached") as mock_set_fat:
                 with pytest.raises(IOError, match="No space in parent directory /"): # Match actual exception
                    fs.create_directory("/NEWDIR")
                 mock_find_entry.assert_called_once_with(0) # Check it looked in root
                 mock_find_cluster.assert_called()
                 # Check if the allocated cluster was freed during cleanup
                 mock_set_fat.assert_any_call(5, 0)

def test_14_fat12_odd_cluster_read_write(mock_fs_setup):
    fs, mock_state = mock_fs_setup
    cluster = 3
    value_to_write = 0xABC
    # Ensure initial state is loaded
    fs.fat_cache = None
    assert fs._load_fat_cache(), "Failed to load FAT cache initially"

    # Write using the filesystem method
    fs._set_fat_entry_cached(cluster, value_to_write)
    assert fs.fat_dirty

    # Simulate writing back to disk
    fs._commit_fat() # This uses mock_disk.write_sectors
    assert not fs.fat_dirty, "FAT should be clean after commit"

    # Simulate reloading from disk
    fs.fat_cache = None # Invalidate cache
    assert fs._load_fat_cache(), "Failed to load FAT cache after commit"
    read_value = fs._read_fat_entry_cached(cluster, load_if_missing=False) # Read from loaded cache

    assert read_value == value_to_write
    assert not fs.fat_dirty # Should be clean after load

def test_15_fat12_even_cluster_read_write(mock_fs_setup):
    fs, mock_state = mock_fs_setup
    cluster = 2
    value_to_write = 0x321
    # Ensure initial state is loaded
    fs.fat_cache = None
    assert fs._load_fat_cache(), "Failed to load FAT cache initially"

    # Write using the filesystem method
    fs._set_fat_entry_cached(cluster, value_to_write)
    assert fs.fat_dirty

    # Simulate writing back to disk
    fs._commit_fat() # This uses mock_disk.write_sectors
    assert not fs.fat_dirty, "FAT should be clean after commit"

    # Simulate reloading from disk
    fs.fat_cache = None # Invalidate cache
    assert fs._load_fat_cache(), "Failed to load FAT cache after commit"
    read_value = fs._read_fat_entry_cached(cluster, load_if_missing=False) # Read from loaded cache

    assert read_value == value_to_write
    assert not fs.fat_dirty # Should be clean after load


def test_16_find_free_cluster(mock_fs_setup):
    fs, mock_state = mock_fs_setup
    # Load cache explicitly for manipulation
    fs.fat_cache = None
    fs._load_fat_cache()
    # Manipulate the cache
    fs._set_fat_entry_cached(2, 0xFFF)
    fs._set_fat_entry_cached(3, 0xFFF)
    fs._set_fat_entry_cached(4, 0) # Make sure 4 is explicitly free
    # Find free cluster using the method
    free_cluster = fs._find_free_cluster()
    assert free_cluster == 4
    # Manipulate again
    fs._set_fat_entry_cached(4, 0xFFF)
    free_cluster = fs._find_free_cluster()
    assert free_cluster == 5 # Assuming 5 was initially free

def test_17_find_free_cluster_none_free(mock_fs_setup):
    fs, mock_state = mock_fs_setup
    # Load cache explicitly
    fs.fat_cache = None
    fs._load_fat_cache()
    # Fill the cache
    for i in range(2, fs.num_clusters + 2):
        fs._set_fat_entry_cached(i, 0xFFF)
    # Find free cluster
    free_cluster = fs._find_free_cluster()
    assert free_cluster is None

def test_18_fs_multi_sector_read_write_edge(mock_fs_setup):
    fs, mock_state = mock_fs_setup
    bps = fs.boot_sector.bytes_per_sector
    # Use an offset relative to the known data area start
    base_offset = fs.data_area_start_offset
    test_offset = base_offset + bps - 50 # Crosses first sector boundary
    test_len = 150 # Crosses into the second sector
    test_data = bytes([i % 256 for i in range(test_len)])

    # Perform the write using the filesystem method
    fs._write_bytes(test_offset, test_data)

    # Verify using the filesystem read method
    read_back_data = fs._read_bytes(test_offset, test_len)
    assert read_back_data == test_data

    # Optional: Verify the mock state directly (less ideal but useful for debugging mock)
    start_lba = test_offset // bps
    end_lba = (test_offset + test_len - 1) // bps
    assert start_lba in mock_state["written_lba_data"], f"LBA {start_lba} not found in mock write cache"
    bytes_in_first_lba = bps - (test_offset % bps)
    offset_in_lba = test_offset % bps
    assert mock_state["written_lba_data"][start_lba][offset_in_lba : offset_in_lba + bytes_in_first_lba] == test_data[:bytes_in_first_lba]
    if start_lba != end_lba:
        assert end_lba in mock_state["written_lba_data"], f"LBA {end_lba} not found in mock write cache"
        bytes_in_second_lba = test_len - bytes_in_first_lba
        assert mock_state["written_lba_data"][end_lba][:bytes_in_second_lba] == test_data[bytes_in_first_lba:]

def test_19_fs_parse_corrupt_entry(mock_fs_setup):
    fs, _ = mock_fs_setup
    base_entry = fs._create_directory_entry_bytes(
        "GOODFILE.TXT", False, 5, 100, datetime.datetime.now()
    )

    # Test invalid starting character
    entry_bad_char = bytearray(base_entry)
    entry_bad_char[0] = ord("*") # Invalid 8.3 char
    assert fs._parse_single_directory_entry(bytes(entry_bad_char)) is None

    # Test invalid date components
    entry_bad_date = bytearray(base_entry)
    # Create an invalid month (13)
    date_val = (((2020 - 1980) & 0x7F) << 9) | (13 << 5) | 1
    struct.pack_into("<H", entry_bad_date, 24, date_val)
    parsed_bad_date = fs._parse_single_directory_entry(bytes(entry_bad_date))
    assert parsed_bad_date is not None
    # Check if it defaulted to the minimum valid date/time
    assert parsed_bad_date.datetime.year == 1980
    assert parsed_bad_date.datetime.month == 1
    assert parsed_bad_date.datetime.day == 1
    assert parsed_bad_date.datetime.hour == 0
    assert parsed_bad_date.datetime.minute == 0
    assert parsed_bad_date.datetime.second == 0


    # Test LFN entry (should be skipped)
    entry_lfn = bytearray(base_entry)
    entry_lfn[11] = ATTR_LONG_NAME
    assert fs._parse_single_directory_entry(bytes(entry_lfn)) is None

    # Test Volume ID entry
    entry_volid = bytearray(32) # Start fresh for clarity
    vol_label_padded = b"VOL LABEL".ljust(11)
    entry_volid[0:11] = vol_label_padded
    entry_volid[11] = ATTR_VOLUME_ID
    # Add a plausible datetime
    time_val = ((10 & 0x1F) << 11) | ((30 & 0x3F) << 5) | ((0 // 2) & 0x1F)
    date_val = (((2023 - 1980) & 0x7F) << 9) | ((10 & 0x0F) << 5) | (26 & 0x1F)
    struct.pack_into("<H", entry_volid, 22, time_val)
    struct.pack_into("<H", entry_volid, 24, date_val)

    parsed_vol = fs._parse_single_directory_entry(bytes(entry_volid))
    assert parsed_vol is not None
    assert parsed_vol.name == "VOL LABEL"
    assert parsed_vol.attributes == "VOL"
    assert parsed_vol.is_dir is False # Volume ID is not a directory
    assert parsed_vol.size == 0
    assert parsed_vol.datetime == datetime.datetime.min # Should ignore file datetime for VOL
