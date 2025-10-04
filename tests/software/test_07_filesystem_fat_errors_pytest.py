# tests/software/test_07_filesystem_fat_errors_pytest.py
"""
Tests for the FAT12 Filesystem focusing on error handling and edge cases.

This test suite uses a deeply mocked Disk object to simulate the underlying
storage, allowing for precise control over the filesystem's state. It verifies
the FATFilesystem class's ability to correctly handle various error conditions
without relying on an actual disk image file.

The tests cover:
- Invalid filename and directory name validation.
- Handling of disk full (no free clusters) and directory full conditions.
- Correct behavior when deleting non-empty directories.
- Correct reading and writing of FAT entries for both odd and even clusters.
- Proper handling of I/O operations that span multiple sectors.
- Graceful parsing of corrupt or non-standard directory entries.
"""
import datetime
import struct
import sys
from pathlib import Path
from typing import Any, Dict, Generator, Tuple
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

# Add the source directory to the Python path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers import DiskIODriver
from fatfloppy.core.filesystems.fat12_fs import FATFilesystem, FileInfo
from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat

# --- Constants for a standard 720KB floppy disk format ---
# Boot Sector and FAT layout parameters
BYTES_PER_SECTOR: int = 512
SECTORS_PER_CLUSTER: int = 2
RESERVED_SECTORS: int = 1
NUM_FATS: int = 2
ROOT_ENTRIES: int = 112
TOTAL_SECTORS: int = 1440
SECTORS_PER_FAT: int = 3
SECTORS_PER_TRACK: int = 9
NUM_HEADS: int = 2

# Physical geometry parameters
CYLINDERS: int = TOTAL_SECTORS // (SECTORS_PER_TRACK * NUM_HEADS)
RPM: int = 300
RATE: int = 250  # 250 kbps for 720k DD
ENCODING: str = "MFM"
GAP3: int = 84
INTERLEAVE: int = 1

# Derived layout constants
ALLOCATION_UNIT_SIZE: int = BYTES_PER_SECTOR * SECTORS_PER_CLUSTER
ROOT_DIR_SECTORS: int = (ROOT_ENTRIES * 32 + BYTES_PER_SECTOR - 1) // BYTES_PER_SECTOR
FAT_SIZE_BYTES: int = SECTORS_PER_FAT * BYTES_PER_SECTOR
FAT_AREA_SIZE_BYTES: int = NUM_FATS * FAT_SIZE_BYTES
ROOT_DIR_SIZE_BYTES: int = ROOT_DIR_SECTORS * BYTES_PER_SECTOR

# Memory offsets for different disk areas
BOOT_SECTOR_OFFSET: int = 0
FAT_START_OFFSET: int = RESERVED_SECTORS * BYTES_PER_SECTOR
FAT_END_OFFSET: int = FAT_START_OFFSET + FAT_AREA_SIZE_BYTES
ROOT_DIR_START_OFFSET: int = FAT_END_OFFSET
ROOT_DIR_END_OFFSET: int = ROOT_DIR_START_OFFSET + ROOT_DIR_SIZE_BYTES
DATA_AREA_START_OFFSET: int = ROOT_DIR_END_OFFSET

# Data area calculation
TOTAL_BYTES: int = TOTAL_SECTORS * BYTES_PER_SECTOR
DATA_AREA_BYTES: int = TOTAL_BYTES - DATA_AREA_START_OFFSET
NUM_CLUSTERS: int = DATA_AREA_BYTES // ALLOCATION_UNIT_SIZE if ALLOCATION_UNIT_SIZE > 0 else 0

# FAT attribute constants
ATTR_READ_ONLY: int = 0x01
ATTR_HIDDEN: int = 0x02
ATTR_SYSTEM: int = 0x04
ATTR_VOLUME_ID: int = 0x08
ATTR_DIRECTORY: int = 0x10
ATTR_ARCHIVE: int = 0x20
ATTR_LONG_NAME: int = ATTR_READ_ONLY | ATTR_HIDDEN | ATTR_SYSTEM | ATTR_VOLUME_ID


@pytest.fixture(scope="function")
def mock_fs_setup(request: pytest.FixtureRequest) -> Generator[Tuple[FATFilesystem, Dict[str, Any]], None, None]:
    """
    Sets up a FATFilesystem with a fully mocked Disk backend.

    This fixture creates an in-memory representation of a 720KB floppy disk,
    including the boot sector, FATs, and root directory. It then creates mock
    read and write methods for the Disk object that interact with this in-memory
    data, allowing tests to run without any actual file I/O.

    Args:
        request: The pytest request object, used for logging.

    Yields:
        A tuple containing:
        - An initialized FATFilesystem instance.
        - A 'mock_state' dictionary holding the mutable in-memory disk data
          (boot_sector_data, fat_data, root_dir_data, written_lba_data).
    """
    print(f"\n--- [Fixture Setup] Mocking FS for test: {request.node.name} ---")
    mock_driver = MagicMock(spec=DiskIODriver)
    mock_disk = MagicMock(spec=Disk)
    mock_disk.driver = mock_driver

    # Define the physical geometry for the mock disk
    mock_track_format = TrackFormat(
        track_start=0,
        track_end=CYLINDERS - 1,
        head_start=0,
        head_end=NUM_HEADS - 1,
        sectors_per_track=SECTORS_PER_TRACK,
        encoding=ENCODING,
        rate=RATE,
        gap3_bytes=GAP3,
        interleave=INTERLEAVE,
    )
    mock_disk_geometry = PhysicalFormat(
        cylinders=CYLINDERS,
        heads=NUM_HEADS,
        rpm=RPM,
        heads_inverted=False,
        bytes_per_sector=BYTES_PER_SECTOR,
        track_formats=[mock_track_format],
    )
    # Use PropertyMock as the geometry property is accessed multiple times
    type(mock_disk).physical_format = PropertyMock(return_value=mock_disk_geometry)

    # --- Create initial in-memory representations of disk structures ---
    boot_sector_data = bytearray(BYTES_PER_SECTOR)
    struct.pack_into("<H", boot_sector_data, 0x0B, BYTES_PER_SECTOR)
    boot_sector_data[0x0D] = SECTORS_PER_CLUSTER
    struct.pack_into("<H", boot_sector_data, 0x0E, RESERVED_SECTORS)
    boot_sector_data[0x10] = NUM_FATS
    struct.pack_into("<H", boot_sector_data, 0x11, ROOT_ENTRIES)
    struct.pack_into("<H", boot_sector_data, 0x13, TOTAL_SECTORS)
    struct.pack_into("<I", boot_sector_data, 0x20, 0)  # Total sectors 32-bit (0 means use 16-bit value)
    boot_sector_data[0x15] = 0xF9  # Media descriptor for 720k
    struct.pack_into("<H", boot_sector_data, 0x16, SECTORS_PER_FAT)
    struct.pack_into("<H", boot_sector_data, 0x18, SECTORS_PER_TRACK)
    struct.pack_into("<H", boot_sector_data, 0x1A, NUM_HEADS)
    struct.pack_into("<I", boot_sector_data, 0x1C, 0)  # Hidden sectors
    struct.pack_into("<H", boot_sector_data, 0x1FE, 0xAA55)  # Boot signature

    fat_data = bytearray(FAT_SIZE_BYTES)
    fat_data[0:3] = bytes([0xF9, 0xFF, 0xFF])  # FAT ID and reserved entries

    root_dir_data = bytearray(ROOT_DIR_SIZE_BYTES)  # Initially empty
    written_lba_data: Dict[int, bytes] = {}  # Cache for data written by mock_write_sectors

    def mock_lba_to_chs(lba: int) -> Tuple[int, int, int]:
        """Mocks the LBA to CHS conversion based on the fixture's geometry."""
        geom = mock_disk_geometry
        spt = geom.get_sectors_per_track(0, 0)
        heads = geom.heads
        if spt <= 0 or heads <= 0:
            raise ValueError("Invalid geometry in mock_lba_to_chs")
        max_lba = geom.total_sectors - 1
        if not 0 <= lba <= max_lba:
            raise IndexError(f"LBA {lba} out of bounds (0-{max_lba})")
        sector = (lba % spt) + 1
        temp = lba // spt
        head = temp % heads
        cylinder = temp // heads
        return cylinder, head, sector

    mock_disk.lba_to_chs = MagicMock(side_effect=mock_lba_to_chs)

    def mock_read_sectors(start_c: int, start_h: int, start_s: int, num_sectors: int) -> bytes:
        """Mocks reading sectors by pulling data from in-memory representations."""
        result = bytearray()
        geom = mock_disk_geometry
        try:
            start_lba = geom.chs_to_lba(start_c, start_h, start_s)
        except (ValueError, IndexError) as e:
            print(f"DEBUG: Error calculating start LBA in mock_read_sectors: C={start_c} H={start_h} S={start_s}, Error: {e}")
            return b"\x00" * num_sectors * geom.bytes_per_sector

        for i in range(num_sectors):
            current_lba = start_lba + i
            if not 0 <= current_lba < geom.total_sectors:
                print(f"DEBUG: mock_read_sectors attempted read beyond disk bounds: LBA {current_lba}")
                data_chunk = b"\x00" * geom.bytes_per_sector
            else:
                current_offset = current_lba * geom.bytes_per_sector
                # Prioritize recently written data from the write cache
                if current_lba in written_lba_data:
                    data_chunk = written_lba_data[current_lba]
                elif current_lba == 0:
                    data_chunk = boot_sector_data
                elif FAT_START_OFFSET <= current_offset < FAT_END_OFFSET:
                    rel_offset = current_offset - FAT_START_OFFSET
                    data_chunk = fat_data[rel_offset : rel_offset + geom.bytes_per_sector]
                elif ROOT_DIR_START_OFFSET <= current_offset < ROOT_DIR_END_OFFSET:
                    rel_offset = current_offset - ROOT_DIR_START_OFFSET
                    data_chunk = root_dir_data[rel_offset : rel_offset + geom.bytes_per_sector]
                else:
                    data_chunk = b"\x00" * geom.bytes_per_sector
            result.extend(data_chunk.ljust(geom.bytes_per_sector, b"\x00"))
        return bytes(result)

    def mock_write_sectors(start_c: int, start_h: int, start_s: int, data: bytes) -> None:
        """Mocks writing sectors to an in-memory cache and updates core data structures."""
        geom = mock_disk_geometry
        try:
            start_lba = geom.chs_to_lba(start_c, start_h, start_s)
        except (ValueError, IndexError) as e:
            print(f"DEBUG: Error calculating start LBA in mock_write_sectors: C={start_c} H={start_h} S={start_s}, Error: {e}")
            return

        num_sectors = (len(data) + geom.bytes_per_sector - 1) // geom.bytes_per_sector
        for i in range(num_sectors):
            current_lba = start_lba + i
            if not 0 <= current_lba < geom.total_sectors:
                print(f"DEBUG: mock_write_sectors attempted write beyond disk bounds: LBA {current_lba}")
                continue

            chunk_start = i * geom.bytes_per_sector
            chunk = data[chunk_start : chunk_start + geom.bytes_per_sector].ljust(geom.bytes_per_sector, b"\x00")
            written_lba_data[current_lba] = chunk
            current_offset = current_lba * geom.bytes_per_sector

            if FAT_START_OFFSET <= current_offset < FAT_END_OFFSET:
                rel_offset = current_offset - FAT_START_OFFSET
                if rel_offset + len(chunk) <= len(fat_data):
                    fat_data[rel_offset : rel_offset + len(chunk)] = chunk
            elif ROOT_DIR_START_OFFSET <= current_offset < ROOT_DIR_END_OFFSET:
                rel_offset = current_offset - ROOT_DIR_START_OFFSET
                if rel_offset + len(chunk) <= len(root_dir_data):
                    root_dir_data[rel_offset : rel_offset + len(chunk)] = chunk

    # Configure the mock disk object's methods
    mock_disk.read_sector.side_effect = lambda c, h, s: mock_read_sectors(c, h, s, 1)
    mock_disk.read_sectors.side_effect = mock_read_sectors
    mock_disk.write_sector.side_effect = lambda c, h, s, data: mock_write_sectors(c, h, s, data)
    mock_disk.write_sectors.side_effect = mock_write_sectors
    mock_disk.flush.return_value = None

    # Crucially, ensure read_sector for initialization has the right return value
    mock_disk.read_sector.return_value = bytes(boot_sector_data)

    # Initialize the filesystem, which will call the mocked read_sector
    fs = FATFilesystem(mock_disk)
    if fs.get_validity_score() < fs.VALIDITY_THRESHOLD:
        pytest.fail("FATFilesystem failed to initialize in mock_fs_setup fixture.")

    mock_state = {
        "boot_sector_data": boot_sector_data,
        "fat_data": fat_data,
        "root_dir_data": root_dir_data,
        "written_lba_data": written_lba_data,
    }
    yield fs, mock_state
    print(f"--- [Fixture Teardown] Mock FS test: {request.node.name} ---")


def test_01_write_file_invalid_name(mock_fs_setup: Tuple[FATFilesystem, Dict[str, Any]]) -> None:
    """
    Tests that write_file raises ValueError for various invalid 8.3 filenames.
    """
    fs, _ = mock_fs_setup
    invalid_names = [
        "BAD NAME.TXT", "TOOLONGNAM.TXT", "FILE.TOOLONGEXT",
        "COM1", ".BADNAME", "GOOD.", "INVALID*CHAR",
    ]
    for name in invalid_names:
        with pytest.raises(ValueError, match="Invalid 8.3 filename"):
            fs.write_file(name, b"data")


def test_02_create_dir_invalid_name(mock_fs_setup: Tuple[FATFilesystem, Dict[str, Any]]) -> None:
    """
    Tests that create_directory raises ValueError for various invalid 8.3 directory names.
    """
    fs, _ = mock_fs_setup
    invalid_names = [
        "BAD NAME", "TOOLONGDIRNAME", "DIR.EXT",
        "COM1", ".BADDIR", "GOODDIR.", "INVALID?CHAR",
    ]
    for name in invalid_names:
        with pytest.raises(ValueError, match="Invalid 8.3 directory name"):
            fs.create_directory(name)


def test_03_delete_non_empty_directory(mock_fs_setup: Tuple[FATFilesystem, Dict[str, Any]]) -> None:
    """
    Tests that deleting a directory simulated to be non-empty raises an OSError.
    """
    fs, mock_state = mock_fs_setup
    dir_name = "DELDIR"
    dir_path = f"/{dir_name}"
    dir_cluster = 5

    # Arrange: Create the directory entry and simulate it being non-empty
    deldir_entry_bytes = fs._create_directory_entry_bytes(dir_name, True, dir_cluster, 0, datetime.datetime.now())
    mock_state["root_dir_data"][:32] = deldir_entry_bytes
    fs._set_fat_entry_cached(dir_cluster, 0xFFF)
    fs._commit_fat()
    fs.fat_cache = None

    mock_file_entry = FileInfo("dummy.txt", 10, False, datetime.datetime.now(), "-A-", 6)
    original_list_cluster = fs._list_directory_by_cluster

    def mock_list_cluster_side_effect(cluster: int) -> list:
        if cluster == dir_cluster:
            return [mock_file_entry]  # Pretend it has a file
        if fs.fat_cache is None:
            fs._load_fat_cache()
        return original_list_cluster(cluster)

    # Act & Assert
    with patch.object(fs, "_list_directory_by_cluster", side_effect=mock_list_cluster_side_effect) as mock_list_func:
        with pytest.raises(OSError, match=f"Could not verify directory contents before deleting: {dir_path}"):
            fs.delete(dir_path)

        mock_list_func.assert_any_call(dir_cluster)
        reloaded_root_data = fs._read_bytes(ROOT_DIR_START_OFFSET, 32)
        assert reloaded_root_data[0] != 0xE5  # Check entry was NOT deleted


def test_09_write_no_free_clusters(mock_fs_setup: Tuple[FATFilesystem, Dict[str, Any]]) -> None:
    """
    Tests that writing a file when no free clusters are available raises an IOError.
    """
    fs, _ = mock_fs_setup
    with patch.object(fs, "_find_free_cluster", return_value=None) as mock_find_free:
        with pytest.raises(IOError, match="Not enough free space"):
            fs.write_file("/TEST.TXT", b"some data")
        mock_find_free.assert_called()


def test_10_write_no_free_dir_entry_root(mock_fs_setup: Tuple[FATFilesystem, Dict[str, Any]]) -> None:
    """
    Tests that writing a file to a full root directory raises an IOError.
    """
    fs, _ = mock_fs_setup
    with patch.object(fs, "_find_free_directory_entry", return_value=None) as mock_find_entry, \
         patch.object(fs, "_allocate_cluster_chain", return_value=[5]) as mock_alloc, \
         patch.object(fs, "_free_cluster_chain") as mock_free:
        with pytest.raises(IOError, match="No space in directory /"):
            fs.write_file("/TEST.TXT", b"data")
        mock_find_entry.assert_called_once_with(0)  # Check in root
        mock_alloc.assert_called_once_with(1)
        mock_free.assert_called_once_with(5)  # Check cleanup


def test_11_write_no_free_dir_entry_subdir(mock_fs_setup: Tuple[FATFilesystem, Dict[str, Any]]) -> None:
    """
    Tests that writing a file to a full subdirectory raises an IOError.
    """
    fs, mock_state = mock_fs_setup
    subdir_name = "SUB"
    subdir_path = f"/{subdir_name}"
    subdir_cluster = 5

    # Arrange: Create the subdirectory entry in the root directory
    subdir_entry = fs._create_directory_entry_bytes(subdir_name, True, subdir_cluster, 0, datetime.datetime.now())
    mock_state["root_dir_data"][0:32] = subdir_entry
    fs._set_fat_entry_cached(subdir_cluster, 0xFFF)
    fs._commit_fat()
    fs.fat_cache = None

    # Act & Assert
    with patch.object(fs, "_find_free_directory_entry", return_value=None) as mock_find_entry, \
         patch.object(fs, "_allocate_cluster_chain", return_value=[6]) as mock_alloc, \
         patch.object(fs, "_free_cluster_chain") as mock_free:
        with pytest.raises(IOError, match=f"No space in directory {subdir_path}"):
            fs.write_file(f"{subdir_path}/TEST.TXT", b"data")
        mock_find_entry.assert_any_call(subdir_cluster)
        mock_alloc.assert_called_once_with(1)
        mock_free.assert_called_once_with(6)


def test_12_create_dir_no_free_clusters(mock_fs_setup: Tuple[FATFilesystem, Dict[str, Any]]) -> None:
    """
    Tests that creating a directory when no free clusters are available raises an IOError.
    """
    fs, _ = mock_fs_setup
    with patch.object(fs, "_find_free_cluster", return_value=None) as mock_find_free:
        with pytest.raises(IOError, match="No free clusters available"):
            fs.create_directory("/NEWDIR")
        mock_find_free.assert_called()


def test_13_create_dir_no_free_entry(mock_fs_setup: Tuple[FATFilesystem, Dict[str, Any]]) -> None:
    """
    Tests that creating a directory in a full parent directory raises an IOError.
    """
    fs, _ = mock_fs_setup
    with patch.object(fs, "_find_free_directory_entry", return_value=None) as mock_find_entry, \
         patch.object(fs, "_find_free_cluster", return_value=5) as mock_find_cluster, \
         patch.object(fs, "_set_fat_entry_cached") as mock_set_fat:
        with pytest.raises(IOError, match="No space in parent directory /"):
            fs.create_directory("/NEWDIR")
        mock_find_entry.assert_called_once_with(0)  # Check in root
        mock_find_cluster.assert_called()
        mock_set_fat.assert_any_call(5, 0)  # Check allocated cluster was freed


def test_14_fat12_odd_cluster_read_write(mock_fs_setup: Tuple[FATFilesystem, Dict[str, Any]]) -> None:
    """
    Tests the logic for reading/writing an entry for an odd cluster number in the FAT.
    """
    fs, _ = mock_fs_setup
    cluster = 3
    value_to_write = 0xABC
    fs.fat_cache = None
    fs._load_fat_cache()

    fs._set_fat_entry_cached(cluster, value_to_write)
    assert fs.fat_dirty
    fs._commit_fat()
    assert not fs.fat_dirty

    fs.fat_cache = None
    fs._load_fat_cache()
    read_value = fs._read_fat_entry_cached(cluster, load_if_missing=False)

    assert read_value == value_to_write
    assert not fs.fat_dirty


def test_15_fat12_even_cluster_read_write(mock_fs_setup: Tuple[FATFilesystem, Dict[str, Any]]) -> None:
    """
    Tests the logic for reading/writing an entry for an even cluster number in the FAT.
    """
    fs, _ = mock_fs_setup
    cluster = 2
    value_to_write = 0x321
    fs.fat_cache = None
    fs._load_fat_cache()

    fs._set_fat_entry_cached(cluster, value_to_write)
    assert fs.fat_dirty
    fs._commit_fat()
    assert not fs.fat_dirty

    fs.fat_cache = None
    fs._load_fat_cache()
    read_value = fs._read_fat_entry_cached(cluster, load_if_missing=False)

    assert read_value == value_to_write
    assert not fs.fat_dirty


def test_16_find_free_cluster(mock_fs_setup: Tuple[FATFilesystem, Dict[str, Any]]) -> None:
    """
    Tests that _find_free_cluster correctly identifies the next available cluster.
    """
    fs, _ = mock_fs_setup
    fs.fat_cache = None
    fs._load_fat_cache()

    # Manipulate the cache to make clusters 2 and 3 used, and 4 free.
    fs._set_fat_entry_cached(2, 0xFFF)
    fs._set_fat_entry_cached(3, 0xFFF)
    fs._set_fat_entry_cached(4, 0)
    assert fs._find_free_cluster() == 4

    # Now make cluster 4 used and check that 5 is found.
    fs._set_fat_entry_cached(4, 0xFFF)
    assert fs._find_free_cluster() == 5


def test_17_find_free_cluster_none_free(mock_fs_setup: Tuple[FATFilesystem, Dict[str, Any]]) -> None:
    """
    Tests that _find_free_cluster returns None when the disk is full.
    """
    fs, _ = mock_fs_setup
    fs.fat_cache = None
    fs._load_fat_cache()

    # Fill the entire FAT cache
    for i in range(2, fs.num_clusters + 2):
        fs._set_fat_entry_cached(i, 0xFFF)

    assert fs._find_free_cluster() is None


def test_18_fs_multi_sector_read_write_edge(mock_fs_setup: Tuple[FATFilesystem, Dict[str, Any]]) -> None:
    """
    Tests reading and writing a block of data that crosses a sector boundary.
    """
    fs, mock_state = mock_fs_setup
    bps = fs.boot_sector.bytes_per_sector
    base_offset = fs.data_area_start_offset
    test_offset = base_offset + bps - 50  # Start 50 bytes before end of a sector
    test_len = 150  # Write 150 bytes, crossing into the next sector
    test_data = bytes([i % 256 for i in range(test_len)])

    fs._write_bytes(test_offset, test_data)
    read_back_data = fs._read_bytes(test_offset, test_len)

    assert read_back_data == test_data

    # Optional verification of the underlying mock state
    start_lba = test_offset // bps
    end_lba = (test_offset + test_len - 1) // bps
    assert start_lba in mock_state["written_lba_data"]
    bytes_in_first_lba = bps - (test_offset % bps)
    offset_in_lba = test_offset % bps
    assert mock_state["written_lba_data"][start_lba][offset_in_lba:] == test_data[:bytes_in_first_lba]
    if start_lba != end_lba:
        assert end_lba in mock_state["written_lba_data"]
        bytes_in_second_lba = test_len - bytes_in_first_lba
        assert mock_state["written_lba_data"][end_lba][:bytes_in_second_lba] == test_data[bytes_in_first_lba:]


def test_19_fs_parse_corrupt_entry(mock_fs_setup: Tuple[FATFilesystem, Dict[str, Any]]) -> None:
    """
    Tests that the directory entry parser handles various corrupt or special entries.
    """
    fs, _ = mock_fs_setup
    base_entry = fs._create_directory_entry_bytes("GOODFILE.TXT", False, 5, 100, datetime.datetime.now())

    # Test invalid starting character (should be skipped)
    entry_bad_char = bytearray(base_entry)
    entry_bad_char[0] = ord("*")
    assert fs._parse_single_directory_entry(bytes(entry_bad_char)) is None

    # Test invalid date, which should parse with a default minimal datetime
    entry_bad_date = bytearray(base_entry)
    date_val = (((2020 - 1980) & 0x7F) << 9) | (13 << 5) | 1  # Invalid month 13
    struct.pack_into("<H", entry_bad_date, 24, date_val)
    parsed_bad_date = fs._parse_single_directory_entry(bytes(entry_bad_date))
    assert parsed_bad_date is not None
    assert parsed_bad_date.datetime == datetime.datetime(1980, 1, 1, 0, 0, 0)

    # Test Long File Name entry (should be skipped)
    entry_lfn = bytearray(base_entry)
    entry_lfn[11] = ATTR_LONG_NAME
    assert fs._parse_single_directory_entry(bytes(entry_lfn)) is None

    # Test Volume ID entry
    entry_volid = bytearray(32)
    entry_volid[0:11] = b"VOL LABEL".ljust(11)
    entry_volid[11] = ATTR_VOLUME_ID
    time_val = ((10 & 0x1F) << 11) | ((30 & 0x3F) << 5) | ((0 // 2) & 0x1F)
    date_val = (((2023 - 1980) & 0x7F) << 9) | ((10 & 0x0F) << 5) | (26 & 0x1F)
    struct.pack_into("<H", entry_volid, 22, time_val)
    struct.pack_into("<H", entry_volid, 24, date_val)

    parsed_vol = fs._parse_single_directory_entry(bytes(entry_volid))
    assert parsed_vol is not None
    assert parsed_vol.name == "VOL LABEL"
    assert parsed_vol.attributes == "VOL"
    assert not parsed_vol.is_dir
    assert parsed_vol.datetime == datetime.datetime.min
