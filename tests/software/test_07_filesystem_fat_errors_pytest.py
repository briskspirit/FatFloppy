# tests/software/test_07_filesystem_fat_errors_pytest.py
import pytest
import sys
import datetime
import struct
from unittest.mock import MagicMock, patch, PropertyMock
from pathlib import Path

# Ensure src is in path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.filesystem import FATFilesystem, FATBootSector, FileInfo
from fatfloppy.core.disk import Disk, DiskGeometry
from fatfloppy.core.drivers import DiskIODriver # Use generic driver mock

# --- Constants (Copied and verified) ---
SECTOR_SIZE = 512; SECTORS_PER_CLUSTER = 2; RESERVED_SECTORS = 1; NUM_FATS = 2
ROOT_ENTRIES = 112; TOTAL_SECTORS = 1440; SECTORS_PER_FAT = 3; SECTORS_PER_TRACK = 9
NUM_HEADS = 2; CLUSTER_SIZE = SECTOR_SIZE * SECTORS_PER_CLUSTER
ROOT_DIR_SECTORS = (ROOT_ENTRIES * 32 + SECTOR_SIZE - 1) // SECTOR_SIZE
FAT_SIZE_BYTES = SECTORS_PER_FAT * SECTOR_SIZE
FAT_AREA_SIZE_BYTES = NUM_FATS * FAT_SIZE_BYTES
ROOT_DIR_SIZE_BYTES = ROOT_DIR_SECTORS * SECTOR_SIZE
BOOT_SECTOR_OFFSET = 0
FAT_START_OFFSET = RESERVED_SECTORS * SECTOR_SIZE
FAT_END_OFFSET = FAT_START_OFFSET + FAT_AREA_SIZE_BYTES
ROOT_DIR_START_OFFSET = FAT_END_OFFSET
ROOT_DIR_END_OFFSET = ROOT_DIR_START_OFFSET + ROOT_DIR_SIZE_BYTES
DATA_AREA_START_OFFSET = ROOT_DIR_END_OFFSET
NUM_CLUSTERS = (TOTAL_SECTORS * SECTOR_SIZE - DATA_AREA_START_OFFSET) // CLUSTER_SIZE
ATTR_READ_ONLY = 0x01; ATTR_HIDDEN = 0x02; ATTR_SYSTEM = 0x04
ATTR_VOLUME_ID = 0x08; ATTR_DIRECTORY = 0x10
ATTR_LONG_NAME = ATTR_READ_ONLY | ATTR_HIDDEN | ATTR_SYSTEM | ATTR_VOLUME_ID

# --- Fixture ---
@pytest.fixture(scope="function")
def mock_fs_setup(request):
    """Sets up a FATFilesystem with mocked disk interactions."""
    print(f"\n--- [Fixture Setup] Mocking FS for test: {request.node.name} ---")
    mock_driver = MagicMock(spec=DiskIODriver)
    mock_disk = MagicMock(spec=Disk)
    mock_disk.driver = mock_driver
    mock_disk_geometry = DiskGeometry(
        cylinders=TOTAL_SECTORS // (SECTORS_PER_TRACK * NUM_HEADS),
        heads=NUM_HEADS,
        sectors_per_track=SECTORS_PER_TRACK,
        sector_size=SECTOR_SIZE
    )
    type(mock_disk).geometry = PropertyMock(return_value=mock_disk_geometry)

    # --- Prepare Mock Data ---
    boot_sector_data = bytearray(SECTOR_SIZE)
    struct.pack_into('<H', boot_sector_data, 0x0B, SECTOR_SIZE); boot_sector_data[0x0D] = SECTORS_PER_CLUSTER
    struct.pack_into('<H', boot_sector_data, 0x0E, RESERVED_SECTORS); boot_sector_data[0x10] = NUM_FATS
    struct.pack_into('<H', boot_sector_data, 0x11, ROOT_ENTRIES); struct.pack_into('<H', boot_sector_data, 0x13, TOTAL_SECTORS)
    struct.pack_into('<I', boot_sector_data, 0x20, 0); boot_sector_data[0x15] = 0xF9
    struct.pack_into('<H', boot_sector_data, 0x16, SECTORS_PER_FAT); struct.pack_into('<H', boot_sector_data, 0x18, SECTORS_PER_TRACK)
    struct.pack_into('<H', boot_sector_data, 0x1A, NUM_HEADS); struct.pack_into('<I', boot_sector_data, 0x1C, 0)
    struct.pack_into('<H', boot_sector_data, 0x1FE, 0xAA55)

    fat_data = bytearray(FAT_SIZE_BYTES)
    fat_data[0:3] = bytes([0xF9, 0xFF, 0xFF]) # FAT ID

    root_dir_data = bytearray(ROOT_DIR_SIZE_BYTES)

    written_lba_data = {} # Store writes {lba: data}

    # --- Mock Disk Methods ---
    def mock_lba_to_chs(lba):
        geom = mock_disk_geometry
        if geom.sectors_per_track <= 0 or geom.heads <= 0: raise ValueError("Bad geom")
        max_lba = geom.total_sectors - 1
        if not (0 <= lba <= max_lba): raise IndexError(f"LBA {lba} out of bounds (0-{max_lba})")
        sector = (lba % geom.sectors_per_track) + 1; temp = lba // geom.sectors_per_track
        head = temp % geom.heads; cylinder = temp // geom.heads
        return cylinder, head, sector
    mock_disk.lba_to_chs = MagicMock(side_effect=mock_lba_to_chs)

    def mock_read_sectors(start_c, start_h, start_s, num_sectors):
        result = bytearray()
        geom = mock_disk_geometry
        try: start_lba = (start_c * geom.heads + start_h) * geom.sectors_per_track + (start_s - 1)
        except: return b'\x00' * num_sectors * SECTOR_SIZE
        for i in range(num_sectors):
            current_lba = start_lba + i; current_offset = current_lba * SECTOR_SIZE
            data_chunk = None
            if current_lba in written_lba_data:
                data_chunk = written_lba_data[current_lba]
            elif current_lba == 0: data_chunk = boot_sector_data
            elif FAT_START_OFFSET <= current_offset < FAT_END_OFFSET:
                rel_offset = current_offset - FAT_START_OFFSET
                if 0 <= rel_offset < len(fat_data): data_chunk = fat_data[rel_offset : min(rel_offset + SECTOR_SIZE, len(fat_data))]
            elif ROOT_DIR_START_OFFSET <= current_offset < ROOT_DIR_END_OFFSET:
                rel_offset = current_offset - ROOT_DIR_START_OFFSET
                if 0 <= rel_offset < len(root_dir_data): data_chunk = root_dir_data[rel_offset : min(rel_offset + SECTOR_SIZE, len(root_dir_data))]
            else: data_chunk = b'\x00' * SECTOR_SIZE
            if data_chunk is None: data_chunk = b'\x00' * SECTOR_SIZE # Default safety
            if len(data_chunk) < SECTOR_SIZE: data_chunk += bytes(SECTOR_SIZE - len(data_chunk))
            result.extend(data_chunk[:SECTOR_SIZE])
        return bytes(result)
    mock_disk.read_sector = MagicMock(side_effect=lambda c,h,s: mock_read_sectors(c,h,s,1))
    mock_disk.read_sectors = MagicMock(side_effect=mock_read_sectors)

    def mock_write_sectors(start_c, start_h, start_s, data):
        geom = mock_disk_geometry
        try: start_lba = (start_c * geom.heads + start_h) * geom.sectors_per_track + (start_s - 1)
        except: return
        num_sectors = (len(data) + SECTOR_SIZE - 1) // SECTOR_SIZE
        for i in range(num_sectors):
            current_lba = start_lba + i; chunk_start = i * SECTOR_SIZE
            chunk_end = min(chunk_start + SECTOR_SIZE, len(data))
            chunk = data[chunk_start:chunk_end]
            if len(chunk) < SECTOR_SIZE: chunk += bytes(SECTOR_SIZE - len(chunk))
            written_lba_data[current_lba] = chunk # Record write
            # --- Update internal mock data stores ---
            current_offset = current_lba * SECTOR_SIZE
            if FAT_START_OFFSET <= current_offset < FAT_END_OFFSET:
                fat1_start = FAT_START_OFFSET
                if fat1_start <= current_offset < fat1_start + FAT_SIZE_BYTES:
                    rel_offset = current_offset - fat1_start
                    if rel_offset + SECTOR_SIZE <= len(fat_data): fat_data[rel_offset:rel_offset+SECTOR_SIZE] = chunk
            elif ROOT_DIR_START_OFFSET <= current_offset < ROOT_DIR_END_OFFSET:
                rel_offset = current_offset - ROOT_DIR_START_OFFSET
                if rel_offset + SECTOR_SIZE <= len(root_dir_data): root_dir_data[rel_offset:rel_offset+SECTOR_SIZE] = chunk
    mock_disk.write_sector = MagicMock(side_effect=lambda c,h,s,data: mock_write_sectors(c,h,s,data))
    mock_disk.write_sectors = MagicMock(side_effect=mock_write_sectors)
    mock_disk.flush = MagicMock()

    # --- Create Filesystem Instance ---
    fs = FATFilesystem(mock_disk)
    assert fs.is_valid(), "Filesystem initialization failed in fixture"
    assert fs.fat_cache is not None, "FAT cache not loaded in fixture"
    assert len(fs.fat_cache) == FAT_SIZE_BYTES, "FAT cache size mismatch in fixture"
    assert fs.fat_cache[:3] == bytes([0xF9, 0xFF, 0xFF]), "FAT ID mismatch in fixture"

    # Store mutable mock data state for tests to potentially modify
    mock_state = {
        'boot_sector_data': boot_sector_data,
        'fat_data': fat_data,
        'root_dir_data': root_dir_data,
        'written_lba_data': written_lba_data
    }

    yield fs, mock_state

    print(f"--- [Fixture Teardown] Mock FS test: {request.node.name} ---")

# --- Tests ---

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
         "BAD NAME", "TOOLONGDIRNAME", "DIR.EXT", "COM1",
         ".BADDIR", "GOODDIR.", "INVALID?CHAR",
    ]
    for name in invalid_names:
         with pytest.raises(ValueError, match="Invalid 8.3 directory name"):
             fs.create_directory(name)

def test_03_delete_non_empty_directory(mock_fs_setup):
    fs, mock_state = mock_fs_setup
    dir_name = "DELDIR"; dir_path = f"/{dir_name}"; dir_cluster = 5
    mock_file_entry = FileInfo(name="dummy.txt", size=10, is_dir=False, datetime=datetime.datetime.now(), attributes="-A-", starting_cluster=6)

    # Patch list_by_cluster behavior for this test
    original_list_cluster = fs._list_directory_by_cluster
    def mock_list_cluster(cluster):
        return [mock_file_entry] if cluster == dir_cluster else original_list_cluster(cluster)
    with patch.object(fs, '_list_directory_by_cluster', side_effect=mock_list_cluster) as mock_list_func:
        # Create entry in mock root dir data
        deldir_entry_bytes = fs._create_directory_entry_bytes(dir_name, True, dir_cluster, 0, datetime.datetime.now())
        mock_state['root_dir_data'][:32] = deldir_entry_bytes

        # Execute test
        with pytest.raises(OSError, match="Directory not empty"):
            fs.delete(dir_path)

        # Assertions
        mock_list_func.assert_any_call(dir_cluster)
        # Verify root dir entry NOT marked deleted
        first_root_sector = mock_state['root_dir_data'][:SECTOR_SIZE] # Read from mutable mock data
        assert first_root_sector[0] != 0xE5

def test_09_write_no_free_clusters(mock_fs_setup):
    fs, _ = mock_fs_setup
    with patch.object(fs, '_find_free_cluster', return_value=None) as mock_find_free:
         with pytest.raises((IOError, OSError), match="Not enough free space"):
             fs.write_file("/TEST.TXT", b"some data")
         mock_find_free.assert_called()

def test_10_write_no_free_dir_entry_root(mock_fs_setup):
    fs, mock_state = mock_fs_setup
    dummy_entry = fs._create_directory_entry_bytes("FILLER.   ", False, 0, 0, datetime.datetime.now())
    for i in range(0, len(mock_state['root_dir_data']), 32):
        mock_state['root_dir_data'][i:i+32] = dummy_entry

    with patch.object(fs, '_allocate_cluster_chain', return_value=[5]) as mock_alloc:
         with patch.object(fs, '_free_cluster_chain') as mock_free:
             with pytest.raises((IOError, OSError), match="No space in directory /"):
                 fs.write_file("/TEST.TXT", b"data")
             mock_alloc.assert_called_once_with(1)
             mock_free.assert_called_once_with(5)

def test_11_write_no_free_dir_entry_subdir(mock_fs_setup):
    fs, mock_state = mock_fs_setup
    subdir_name = "SUB"; subdir_path = f"/{subdir_name}"; subdir_cluster = 5

    subdir_entry_bytes = fs._create_directory_entry_bytes(subdir_name, True, subdir_cluster, 0, datetime.datetime.now())
    mock_state['root_dir_data'][0:32] = subdir_entry_bytes

    subdir_data_offset = fs._cluster_to_offset(subdir_cluster)
    subdir_lba = subdir_data_offset // SECTOR_SIZE
    subdir_lba_end = (subdir_data_offset + fs.cluster_size - 1) // SECTOR_SIZE
    dummy_entry = fs._create_directory_entry_bytes("FILLER.   ", False, 0, 0, datetime.datetime.now())
    full_subdir_cluster_data = dummy_entry * (fs.cluster_size // 32)

    # Make sure writes to the subdir LBA store the 'full' data
    for lba in range(subdir_lba, subdir_lba_end + 1):
        offset_in_full = (lba - subdir_lba) * SECTOR_SIZE
        mock_state['written_lba_data'][lba] = full_subdir_cluster_data[offset_in_full : offset_in_full + SECTOR_SIZE]

    # Mock find free cluster to fail extension
    with patch.object(fs, '_find_free_cluster', return_value=None) as mock_find_free:
         with patch.object(fs, '_allocate_cluster_chain', return_value=[6]) as mock_alloc:
             with patch.object(fs, '_free_cluster_chain') as mock_free:
                  with pytest.raises((IOError, OSError), match=f"No space in directory {subdir_path}"):
                      fs.write_file(f"{subdir_path}/TEST.TXT", b"data")
                  mock_alloc.assert_called_once_with(1)
                  mock_free.assert_called_once_with(6)
                  mock_find_free.assert_called() # Called during extension attempt

def test_12_create_dir_no_free_clusters(mock_fs_setup):
    fs, _ = mock_fs_setup
    with patch.object(fs, '_find_free_cluster', return_value=None) as mock_find_free:
         with pytest.raises((IOError, OSError), match="No free clusters available"):
             fs.create_directory("/NEWDIR")
         mock_find_free.assert_called()

def test_13_create_dir_no_free_entry(mock_fs_setup):
    fs, mock_state = mock_fs_setup
    dummy_entry = fs._create_directory_entry_bytes("FILLER.   ", False, 0, 0, datetime.datetime.now())
    for i in range(0, len(mock_state['root_dir_data']), 32):
        mock_state['root_dir_data'][i:i+32] = dummy_entry

    with patch.object(fs, '_find_free_cluster', return_value=5) as mock_find_cluster:
        with patch.object(fs, '_set_fat_entry_cached') as mock_set_fat:
            with pytest.raises((IOError, OSError), match="No space in parent directory /"):
                 fs.create_directory("/NEWDIR")
            mock_find_cluster.assert_called()
            mock_set_fat.assert_any_call(5, 0) # Cleanup attempt

def test_14_fat12_odd_cluster_read_write(mock_fs_setup):
    fs, mock_state = mock_fs_setup
    cluster = 3; byte_offset = 4
    initial_word = 0xDEF3
    struct.pack_into('<H', mock_state['fat_data'], byte_offset, initial_word)
    fs.fat_cache = mock_state['fat_data'][:] # Sync cache

    value_to_write = 0xABC; expected_word = 0xABC3
    fs._set_fat_entry_cached(cluster, value_to_write)
    written_word = struct.unpack_from('<H', fs.fat_cache, byte_offset)[0]
    assert written_word == expected_word; assert fs.fat_dirty is True

    fs.fat_dirty = False # Reset for read test
    fs.fat_cache = bytearray(FAT_SIZE_BYTES); struct.pack_into('<H', fs.fat_cache, byte_offset, expected_word)
    read_value = fs._read_fat_entry_cached(cluster)
    assert read_value == value_to_write; assert fs.fat_dirty is False

def test_15_fat12_even_cluster_read_write(mock_fs_setup):
    fs, mock_state = mock_fs_setup
    cluster = 2; byte_offset = 3
    initial_word = 0xDE23
    struct.pack_into('<H', mock_state['fat_data'], byte_offset, initial_word)
    fs.fat_cache = mock_state['fat_data'][:] # Sync cache

    value_to_write = 0x321; expected_word = 0xD321
    fs._set_fat_entry_cached(cluster, value_to_write)
    written_word = struct.unpack_from('<H', fs.fat_cache, byte_offset)[0]
    assert written_word == expected_word; assert fs.fat_dirty is True

    fs.fat_dirty = False # Reset for read test
    fs.fat_cache = bytearray(FAT_SIZE_BYTES); struct.pack_into('<H', fs.fat_cache, byte_offset, expected_word)
    read_value = fs._read_fat_entry_cached(cluster)
    assert read_value == value_to_write; assert fs.fat_dirty is False

def test_16_find_free_cluster(mock_fs_setup):
    fs, mock_state = mock_fs_setup
    fs.fat_cache = mock_state['fat_data'][:] # Use mutable fat_data
    fs._set_fat_entry_cached(2, 0xFFF)
    fs._set_fat_entry_cached(3, 0xFFF)
    fs._set_fat_entry_cached(4, 0)
    free_cluster = fs._find_free_cluster()
    assert free_cluster == 4

    fs._set_fat_entry_cached(4, 0xFFF)
    free_cluster = fs._find_free_cluster()
    assert free_cluster == 5

def test_17_find_free_cluster_none_free(mock_fs_setup):
    fs, mock_state = mock_fs_setup
    fs.fat_cache = mock_state['fat_data'][:] # Use mutable fat_data
    for i in range(2, fs.num_clusters + 2):
         fs._set_fat_entry_cached(i, 0xFFF)
    free_cluster = fs._find_free_cluster()
    assert free_cluster is None

def test_18_fs_multi_sector_read_write_edge(mock_fs_setup):
    fs, mock_state = mock_fs_setup
    sector_size = fs.boot_sector.bytes_per_sector
    base_offset = DATA_AREA_START_OFFSET
    test_offset = base_offset + sector_size - 50
    test_len = 150
    test_data = bytes([i % 256 for i in range(test_len)])
    start_lba = test_offset // sector_size
    end_lba = (test_offset + test_len - 1) // sector_size

    fs._write_bytes(test_offset, test_data) # Uses mocked disk methods

    assert start_lba in mock_state['written_lba_data']
    bytes_in_first_lba = sector_size - (test_offset % sector_size)
    offset_in_lba = test_offset % sector_size
    assert mock_state['written_lba_data'][start_lba][offset_in_lba:] == test_data[:bytes_in_first_lba]

    if start_lba != end_lba:
        assert end_lba in mock_state['written_lba_data']
        bytes_in_second_lba = test_len - bytes_in_first_lba
        assert mock_state['written_lba_data'][end_lba][:bytes_in_second_lba] == test_data[bytes_in_first_lba:]

    read_back_data = fs._read_bytes(test_offset, test_len)
    assert read_back_data == test_data

def test_19_fs_parse_corrupt_entry(mock_fs_setup):
    fs, _ = mock_fs_setup
    base_entry = fs._create_directory_entry_bytes("GOODFILE.TXT", False, 5, 100, datetime.datetime.now())

    entry_bad_char = bytearray(base_entry); entry_bad_char[0] = ord('*')
    assert fs._parse_single_directory_entry(bytes(entry_bad_char)) is None

    entry_bad_date = bytearray(base_entry)
    date_val = (((2020 - 1980) & 0x7F) << 9) | (13 << 5) | 1 # Month 13
    struct.pack_into('<H', entry_bad_date, 24, date_val)
    parsed_bad_date = fs._parse_single_directory_entry(bytes(entry_bad_date))
    assert parsed_bad_date is not None
    assert parsed_bad_date.datetime.year == 1980
    assert parsed_bad_date.datetime.month == 1
    assert parsed_bad_date.datetime.day == 1

    entry_lfn = bytearray(base_entry); entry_lfn[11] = ATTR_LONG_NAME
    assert fs._parse_single_directory_entry(bytes(entry_lfn)) is None

    entry_volid = bytearray(base_entry)
    vol_label_padded = b'VOL LABEL'.ljust(11)
    entry_volid[0:11] = vol_label_padded; entry_volid[11] = ATTR_VOLUME_ID
    parsed_vol = fs._parse_single_directory_entry(bytes(entry_volid))
    assert parsed_vol is not None
    assert parsed_vol.name == "VOL LABEL"
    assert parsed_vol.attributes == "VOL"
