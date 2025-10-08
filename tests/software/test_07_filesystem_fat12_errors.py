"""
Tests for the FAT12 Filesystem focusing on error handling and edge cases.

This test suite uses a deeply mocked Disk object to simulate the underlying
storage, allowing for precise control over the filesystem's state.
"""

import datetime
import struct
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers import DiskIODriver
from fatfloppy.core.filesystems.fat12_fs import FATFilesystem, FileInfo
from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat

BYTES_PER_SECTOR = 512
SECTORS_PER_CLUSTER = 2
RESERVED_SECTORS = 1
NUM_FATS = 2
ROOT_ENTRIES = 112
TOTAL_SECTORS = 1440
SECTORS_PER_FAT = 3
SECTORS_PER_TRACK = 9
NUM_HEADS = 2

CYLINDERS = TOTAL_SECTORS // (SECTORS_PER_TRACK * NUM_HEADS)
RPM = 300
RATE = 250
ENCODING = "MFM"
GAP3 = 84
INTERLEAVE = 1

ALLOCATION_UNIT_SIZE = BYTES_PER_SECTOR * SECTORS_PER_CLUSTER
ROOT_DIR_SECTORS = (ROOT_ENTRIES * 32 + BYTES_PER_SECTOR - 1) // BYTES_PER_SECTOR
FAT_SIZE_BYTES = SECTORS_PER_FAT * BYTES_PER_SECTOR
FAT_AREA_SIZE_BYTES = NUM_FATS * FAT_SIZE_BYTES
ROOT_DIR_SIZE_BYTES = ROOT_DIR_SECTORS * BYTES_PER_SECTOR

BOOT_SECTOR_OFFSET = 0
FAT_START_OFFSET = RESERVED_SECTORS * BYTES_PER_SECTOR
FAT_END_OFFSET = FAT_START_OFFSET + FAT_AREA_SIZE_BYTES
ROOT_DIR_START_OFFSET = FAT_END_OFFSET
ROOT_DIR_END_OFFSET = ROOT_DIR_START_OFFSET + ROOT_DIR_SIZE_BYTES
DATA_AREA_START_OFFSET = ROOT_DIR_END_OFFSET

TOTAL_BYTES = TOTAL_SECTORS * BYTES_PER_SECTOR
DATA_AREA_BYTES = TOTAL_BYTES - DATA_AREA_START_OFFSET
NUM_CLUSTERS = (
    DATA_AREA_BYTES // ALLOCATION_UNIT_SIZE if ALLOCATION_UNIT_SIZE > 0 else 0
)

ATTR_READ_ONLY = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_VOLUME_ID = 0x08
ATTR_DIRECTORY = 0x10
ATTR_ARCHIVE = 0x20
ATTR_LONG_NAME = ATTR_READ_ONLY | ATTR_HIDDEN | ATTR_SYSTEM | ATTR_VOLUME_ID


@pytest.fixture(scope="function")
def mock_fs_setup() -> Generator[tuple[FATFilesystem, dict[str, Any]], None, None]:
    """
    Sets up a FATFilesystem with a fully mocked Disk backend.

    Yields:
        Tuple containing FATFilesystem instance and mock_state dictionary.
    """
    mock_driver = MagicMock(spec=DiskIODriver)
    mock_disk = MagicMock(spec=Disk)
    mock_disk.driver = mock_driver

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
    type(mock_disk).physical_format = PropertyMock(return_value=mock_disk_geometry)

    boot_sector_data = bytearray(BYTES_PER_SECTOR)
    struct.pack_into("<H", boot_sector_data, 0x0B, BYTES_PER_SECTOR)
    boot_sector_data[0x0D] = SECTORS_PER_CLUSTER
    struct.pack_into("<H", boot_sector_data, 0x0E, RESERVED_SECTORS)
    boot_sector_data[0x10] = NUM_FATS
    struct.pack_into("<H", boot_sector_data, 0x11, ROOT_ENTRIES)
    struct.pack_into("<H", boot_sector_data, 0x13, TOTAL_SECTORS)
    struct.pack_into("<I", boot_sector_data, 0x20, 0)
    boot_sector_data[0x15] = 0xF9
    struct.pack_into("<H", boot_sector_data, 0x16, SECTORS_PER_FAT)
    struct.pack_into("<H", boot_sector_data, 0x18, SECTORS_PER_TRACK)
    struct.pack_into("<H", boot_sector_data, 0x1A, NUM_HEADS)
    struct.pack_into("<I", boot_sector_data, 0x1C, 0)
    struct.pack_into("<H", boot_sector_data, 0x1FE, 0xAA55)

    fat_data = bytearray(FAT_SIZE_BYTES)
    fat_data[0:3] = bytes([0xF9, 0xFF, 0xFF])

    root_dir_data = bytearray(ROOT_DIR_SIZE_BYTES)
    written_lba_data: dict[int, bytes] = {}

    def mock_lba_to_chs(lba: int) -> tuple[int, int, int]:
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

    def mock_read_sectors(
        start_c: int, start_h: int, start_s: int, num_sectors: int
    ) -> bytes:
        """Mocks reading sectors by pulling data from in-memory representations."""
        result = bytearray()
        geom = mock_disk_geometry
        try:
            start_byte_offset = geom.chs_to_byte_offset(start_c, start_h, start_s)
            start_lba = start_byte_offset // geom.bytes_per_sector
        except (ValueError, IndexError):
            return b"\x00" * num_sectors * geom.bytes_per_sector

        for i in range(num_sectors):
            current_lba = start_lba + i
            if not 0 <= current_lba < geom.total_sectors:
                data_chunk = b"\x00" * geom.bytes_per_sector
            else:
                current_offset = current_lba * geom.bytes_per_sector
                if current_lba in written_lba_data:
                    data_chunk = written_lba_data[current_lba]
                elif current_lba == 0:
                    data_chunk = boot_sector_data
                elif FAT_START_OFFSET <= current_offset < FAT_END_OFFSET:
                    rel_offset = current_offset - FAT_START_OFFSET
                    data_chunk = fat_data[
                        rel_offset : rel_offset + geom.bytes_per_sector
                    ]
                elif ROOT_DIR_START_OFFSET <= current_offset < ROOT_DIR_END_OFFSET:
                    rel_offset = current_offset - ROOT_DIR_START_OFFSET
                    data_chunk = root_dir_data[
                        rel_offset : rel_offset + geom.bytes_per_sector
                    ]
                else:
                    data_chunk = b"\x00" * geom.bytes_per_sector
            result.extend(data_chunk.ljust(geom.bytes_per_sector, b"\x00"))
        return bytes(result)

    def mock_write_sectors(
        start_c: int, start_h: int, start_s: int, data: bytes
    ) -> None:
        """Mocks writing sectors to an in-memory cache and updates core data structures."""
        geom = mock_disk_geometry
        try:
            start_byte_offset = geom.chs_to_byte_offset(start_c, start_h, start_s)
            start_lba = start_byte_offset // geom.bytes_per_sector
        except (ValueError, IndexError):
            return

        num_sectors = (len(data) + geom.bytes_per_sector - 1) // geom.bytes_per_sector
        for i in range(num_sectors):
            current_lba = start_lba + i
            if not 0 <= current_lba < geom.total_sectors:
                continue

            chunk_start = i * geom.bytes_per_sector
            chunk = data[chunk_start : chunk_start + geom.bytes_per_sector].ljust(
                geom.bytes_per_sector, b"\x00"
            )
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

    mock_disk.read_sector.side_effect = lambda c, h, s: mock_read_sectors(c, h, s, 1)
    mock_disk.read_sectors.side_effect = mock_read_sectors
    mock_disk.write_sector.side_effect = lambda c, h, s, data: mock_write_sectors(
        c, h, s, data
    )
    mock_disk.write_sectors.side_effect = mock_write_sectors
    mock_disk.flush.return_value = None

    mock_disk.read_sector.return_value = bytes(boot_sector_data)

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


def test_write_file_invalid_name(
    mock_fs_setup: tuple[FATFilesystem, dict[str, Any]],
) -> None:
    """Tests that write_file raises ValueError for various invalid 8.3 filenames."""
    fs, _ = mock_fs_setup
    invalid_names = [
        "BAD NAME.TXT",
        "TOOLONGNAM.TXT",
        "FILE.TOOLONGEXT",
        "COM1",
        ".BADNAME",
        "GOOD.",
        "INVALID*CHAR",
    ]
    for name in invalid_names:
        with pytest.raises(ValueError, match="Invalid 8.3 filename"):
            fs.write_file(name, b"data")


def test_create_directory_invalid_name(
    mock_fs_setup: tuple[FATFilesystem, dict[str, Any]],
) -> None:
    """Tests that create_directory raises ValueError for invalid 8.3 directory names."""
    fs, _ = mock_fs_setup
    invalid_names = [
        "BAD NAME",
        "TOOLONGDIRNAME",
        "DIR.EXT",
        "COM1",
        ".BADDIR",
        "GOODDIR.",
        "INVALID?CHAR",
    ]
    for name in invalid_names:
        with pytest.raises(ValueError, match="Invalid 8.3 directory name"):
            fs.create_directory(name)


def test_delete_non_empty_directory(
    mock_fs_setup: tuple[FATFilesystem, dict[str, Any]],
) -> None:
    """Tests that deleting a directory simulated to be non-empty raises an OSError."""
    fs, mock_state = mock_fs_setup
    dir_name = "DELDIR"
    dir_path = f"/{dir_name}"
    dir_cluster = 5

    deldir_entry_bytes = fs._create_directory_entry_bytes(
        dir_name, True, dir_cluster, 0, datetime.datetime.now()
    )
    mock_state["root_dir_data"][:32] = deldir_entry_bytes
    fs._set_fat_entry_cached(dir_cluster, 0xFFF)
    fs._commit_fat()
    fs.fat_cache = None

    mock_file_entry = FileInfo(
        "dummy.txt", 10, False, datetime.datetime.now(), "-A-", 6
    )
    original_list_cluster = fs._list_directory_by_cluster

    def mock_list_cluster_side_effect(cluster: int) -> list:
        if cluster == dir_cluster:
            return [mock_file_entry]
        if fs.fat_cache is None:
            fs._load_fat_cache()
        return original_list_cluster(cluster)

    with patch.object(
        fs, "_list_directory_by_cluster", side_effect=mock_list_cluster_side_effect
    ) as mock_list_func:
        with pytest.raises(
            OSError,
            match=f"Could not verify directory contents before deleting: {dir_path}",
        ):
            fs.delete(dir_path)

        mock_list_func.assert_any_call(dir_cluster)
        reloaded_root_data = fs._read_bytes(ROOT_DIR_START_OFFSET, 32)
        assert reloaded_root_data[0] != 0xE5


def test_write_no_free_clusters(
    mock_fs_setup: tuple[FATFilesystem, dict[str, Any]],
) -> None:
    """Tests that writing a file when no free clusters are available raises an IOError."""
    fs, _ = mock_fs_setup
    with patch.object(fs, "_find_free_cluster", return_value=None) as mock_find_free:
        with pytest.raises(IOError, match="Not enough free space"):
            fs.write_file("/TEST.TXT", b"some data")
        mock_find_free.assert_called()


def test_write_no_free_directory_entry_root(
    mock_fs_setup: tuple[FATFilesystem, dict[str, Any]],
) -> None:
    """Tests that writing a file to a full root directory raises an IOError."""
    fs, _ = mock_fs_setup
    with (
        patch.object(
            fs, "_find_free_directory_entry", return_value=None
        ) as mock_find_entry,
        patch.object(fs, "_allocate_cluster_chain", return_value=[5]) as mock_alloc,
        patch.object(fs, "_free_cluster_chain") as mock_free,
    ):
        with pytest.raises(IOError, match="No space in directory /"):
            fs.write_file("/TEST.TXT", b"data")
        mock_find_entry.assert_called_once_with(0)
        mock_alloc.assert_called_once_with(1)
        mock_free.assert_called_once_with(5)


def test_write_no_free_directory_entry_subdir(
    mock_fs_setup: tuple[FATFilesystem, dict[str, Any]],
) -> None:
    """Tests that writing a file to a full subdirectory raises an IOError."""
    fs, mock_state = mock_fs_setup
    subdir_name = "SUB"
    subdir_path = f"/{subdir_name}"
    subdir_cluster = 5

    subdir_entry = fs._create_directory_entry_bytes(
        subdir_name, True, subdir_cluster, 0, datetime.datetime.now()
    )
    mock_state["root_dir_data"][0:32] = subdir_entry
    fs._set_fat_entry_cached(subdir_cluster, 0xFFF)
    fs._commit_fat()
    fs.fat_cache = None

    with (
        patch.object(
            fs, "_find_free_directory_entry", return_value=None
        ) as mock_find_entry,
        patch.object(fs, "_allocate_cluster_chain", return_value=[6]) as mock_alloc,
        patch.object(fs, "_free_cluster_chain") as mock_free,
    ):
        with pytest.raises(IOError, match=f"No space in directory {subdir_path}"):
            fs.write_file(f"{subdir_path}/TEST.TXT", b"data")
        mock_find_entry.assert_any_call(subdir_cluster)
        mock_alloc.assert_called_once_with(1)
        mock_free.assert_called_once_with(6)


def test_create_directory_no_free_clusters(
    mock_fs_setup: tuple[FATFilesystem, dict[str, Any]],
) -> None:
    """Tests that creating a directory when no free clusters are available raises IOError."""
    fs, _ = mock_fs_setup
    with patch.object(fs, "_find_free_cluster", return_value=None) as mock_find_free:
        with pytest.raises(IOError, match="No free clusters available"):
            fs.create_directory("/NEWDIR")
        mock_find_free.assert_called()


def test_create_directory_no_free_entry(
    mock_fs_setup: tuple[FATFilesystem, dict[str, Any]],
) -> None:
    """Tests that creating a directory in a full parent directory raises an IOError."""
    fs, _ = mock_fs_setup
    with (
        patch.object(
            fs, "_find_free_directory_entry", return_value=None
        ) as mock_find_entry,
        patch.object(fs, "_find_free_cluster", return_value=5) as mock_find_cluster,
        patch.object(fs, "_set_fat_entry_cached") as mock_set_fat,
    ):
        with pytest.raises(IOError, match="No space in parent directory /"):
            fs.create_directory("/NEWDIR")
        mock_find_entry.assert_called_once_with(0)
        mock_find_cluster.assert_called()
        mock_set_fat.assert_any_call(5, 0)


def test_fat12_odd_cluster_read_write(
    mock_fs_setup: tuple[FATFilesystem, dict[str, Any]],
) -> None:
    """Tests the logic for reading/writing an entry for an odd cluster number in the FAT."""
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


def test_fat12_even_cluster_read_write(
    mock_fs_setup: tuple[FATFilesystem, dict[str, Any]],
) -> None:
    """Tests the logic for reading/writing an entry for an even cluster number in the FAT."""
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


def test_find_free_cluster(mock_fs_setup: tuple[FATFilesystem, dict[str, Any]]) -> None:
    """Tests that _find_free_cluster correctly identifies the next available cluster."""
    fs, _ = mock_fs_setup
    fs.fat_cache = None
    fs._load_fat_cache()

    fs._set_fat_entry_cached(2, 0xFFF)
    fs._set_fat_entry_cached(3, 0xFFF)
    fs._set_fat_entry_cached(4, 0)
    assert fs._find_free_cluster() == 4

    fs._set_fat_entry_cached(4, 0xFFF)
    assert fs._find_free_cluster() == 5


def test_find_free_cluster_none_free(
    mock_fs_setup: tuple[FATFilesystem, dict[str, Any]],
) -> None:
    """Tests that _find_free_cluster returns None when the disk is full."""
    fs, _ = mock_fs_setup
    fs.fat_cache = None
    fs._load_fat_cache()

    for i in range(2, fs.num_clusters + 2):
        fs._set_fat_entry_cached(i, 0xFFF)

    assert fs._find_free_cluster() is None


def test_multi_sector_read_write_edge(
    mock_fs_setup: tuple[FATFilesystem, dict[str, Any]],
) -> None:
    """Tests reading and writing a block of data that crosses a sector boundary."""
    fs, mock_state = mock_fs_setup
    bps = fs.boot_sector.bytes_per_sector
    base_offset = fs.data_area_start_offset
    test_offset = base_offset + bps - 50
    test_len = 150
    test_data = bytes([i % 256 for i in range(test_len)])

    fs._write_bytes(test_offset, test_data)
    read_back_data = fs._read_bytes(test_offset, test_len)

    assert read_back_data == test_data

    start_lba = test_offset // bps
    end_lba = (test_offset + test_len - 1) // bps
    assert start_lba in mock_state["written_lba_data"]
    bytes_in_first_lba = bps - (test_offset % bps)
    offset_in_lba = test_offset % bps
    assert (
        mock_state["written_lba_data"][start_lba][offset_in_lba:]
        == test_data[:bytes_in_first_lba]
    )
    if start_lba != end_lba:
        assert end_lba in mock_state["written_lba_data"]
        bytes_in_second_lba = test_len - bytes_in_first_lba
        assert (
            mock_state["written_lba_data"][end_lba][:bytes_in_second_lba]
            == test_data[bytes_in_first_lba:]
        )


def test_parse_corrupt_entry(
    mock_fs_setup: tuple[FATFilesystem, dict[str, Any]],
) -> None:
    """Tests that the directory entry parser handles various corrupt or special entries."""
    fs, _ = mock_fs_setup
    base_entry = fs._create_directory_entry_bytes(
        "GOODFILE.TXT", False, 5, 100, datetime.datetime.now()
    )

    entry_bad_char = bytearray(base_entry)
    entry_bad_char[0] = ord("*")
    assert fs._parse_single_directory_entry(bytes(entry_bad_char)) is None

    entry_bad_date = bytearray(base_entry)
    date_val = (((2020 - 1980) & 0x7F) << 9) | (13 << 5) | 1
    struct.pack_into("<H", entry_bad_date, 24, date_val)
    parsed_bad_date = fs._parse_single_directory_entry(bytes(entry_bad_date))
    assert parsed_bad_date is not None
    assert parsed_bad_date.datetime == datetime.datetime(1980, 1, 1, 0, 0, 0)

    entry_lfn = bytearray(base_entry)
    entry_lfn[11] = ATTR_LONG_NAME
    assert fs._parse_single_directory_entry(bytes(entry_lfn)) is None

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
