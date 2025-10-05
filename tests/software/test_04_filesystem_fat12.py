"""
Comprehensive tests for the FAT12 filesystem implementation.

This module tests file and directory operations such as creation, reading,
writing, and deletion. It also covers advanced scenarios including multi-cluster
files, file overwriting, nested directories, and handling of corrupted FAT chains.
"""

import datetime
import shutil
import struct
import sys
from pathlib import Path
from typing import Generator, Optional, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers import IMGImageDriver
from fatfloppy.core.filesystems.fat12_fs import FATFilesystem, FileInfo
from fatfloppy.core.filesystem_registry import FilesystemRegistry

RESOURCE_DIR = Path(__file__).parent.parent / "resources"
EMPTY_IMG_SRC = RESOURCE_DIR / "empty_formatted_144m.img"

_ALL_FORMATS = FilesystemRegistry.get_all_formats()
FMT_144 = _ALL_FORMATS["ibm_3.5_1.44m"]
FMT_720 = _ALL_FORMATS["ibm_3.5_720k"]

FSTestFixture = Tuple[FATFilesystem, Disk]


@pytest.fixture(scope="function")
def fs_setup(tmp_path: Path) -> Generator[FSTestFixture, None, None]:
    """
    Set up a driver, disk, and FATFilesystem with a clean image copy.

    Args:
        tmp_path: The pytest temporary path fixture.

    Yields:
        Tuple containing the initialized FATFilesystem and Disk instances.
    """
    test_img_path = tmp_path / "test_fs_1.44mb.img"

    if not EMPTY_IMG_SRC.exists():
        pytest.skip(f"{EMPTY_IMG_SRC} not found, cannot run filesystem tests.")
    shutil.copy(EMPTY_IMG_SRC, test_img_path)

    driver = IMGImageDriver(str(test_img_path))
    disk = Disk(driver)

    disk.set_geometry(FMT_144.physical_format)
    driver.set_physical_format(FMT_144.physical_format)

    fs = FATFilesystem(disk)
    fs.fat_cache = None
    fs._cached_allocated_clusters = None

    yield fs, disk


def _read_test_fat_entry(fs: FATFilesystem, cluster: int) -> Optional[int]:
    """
    Read a FAT12 entry directly from the disk for verification purposes.

    Args:
        fs: The FATFilesystem instance.
        cluster: The cluster number to look up.

    Returns:
        The value of the FAT entry, or None if an error occurs.
    """
    if fs.get_validity_score() < fs.VALIDITY_THRESHOLD:
        return None

    fat_offset = fs.fat_start_offset + int(cluster * 1.5)
    try:
        if fs.fat_cache:
            byte_offset = int(cluster * 1.5)
            if byte_offset + 1 >= len(fs.fat_cache):
                return None
            value = struct.unpack_from("<H", fs.fat_cache, byte_offset)[0]
        else:
            value_bytes = fs._read_bytes(fat_offset, 2)
            if len(value_bytes) < 2:
                return None
            value = struct.unpack("<H", value_bytes)[0]
    except (IOError, ValueError, struct.error, IndexError):
        return None

    return value & 0x0FFF if cluster % 2 == 0 else value >> 4


def _check_fat_mirror(fs: FATFilesystem) -> None:
    """
    Verify that the first two FATs on the disk are identical.

    Args:
        fs: The FATFilesystem instance to check.
    """
    if fs.get_validity_score() < fs.VALIDITY_THRESHOLD or fs.boot_sector.num_fats < 2:
        return

    fat1_offset = fs.fat_start_offset
    fat_size = fs.boot_sector.sectors_per_fat * fs.boot_sector.bytes_per_sector
    fat2_offset = fat1_offset + fat_size

    try:
        fat1_data = fs._read_bytes(fat1_offset, fat_size)
        fat2_data = fs._read_bytes(fat2_offset, fat_size)
        assert fat1_data == fat2_data, "FAT tables are not mirrored"
    except Exception as e:
        pytest.fail(f"Error during FAT mirror check: {e}")


def test_initialization_valid(fs_setup: FSTestFixture) -> None:
    """Test that the filesystem initializes correctly on a valid 1.44MB image."""
    fs, _ = fs_setup
    assert fs.get_validity_score() >= fs.VALIDITY_THRESHOLD
    assert fs.allocation_unit_size > 0
    assert fs.num_clusters > 0
    assert fs.boot_sector.total_sectors == 2880
    assert fs.allocation_unit_size == 512
    assert fs.num_clusters == 2847


def test_list_root_directory_empty(fs_setup: FSTestFixture) -> None:
    """Test listing the root directory of a freshly formatted image."""
    fs, _ = fs_setup
    entries = fs.list_directory("/")
    assert entries == []


def test_create_file_in_root(fs_setup: FSTestFixture) -> None:
    """Test creating a simple file in the root directory."""
    fs, _ = fs_setup
    filename = "TEST.TXT"
    filedata = b"This is a test file."
    fs.write_file(filename, filedata)

    _check_fat_mirror(fs)

    entries = fs.list_directory("/")
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, FileInfo)
    assert entry.name == filename
    assert entry.size == len(filedata)
    assert not entry.is_dir
    assert entry.starting_cluster is not None and entry.starting_cluster > 1

    fat_val = _read_test_fat_entry(fs, entry.starting_cluster)
    assert fat_val == 0xFFF

    fs._cached_allocated_clusters = None
    free_before, _ = fs.get_free_space()
    fs.write_file("DUMMY.DAT", b"data")
    fs._cached_allocated_clusters = None
    free_after, _ = fs.get_free_space()
    assert free_after < free_before
    fs.delete("DUMMY.DAT")


def test_read_file_in_root(fs_setup: FSTestFixture) -> None:
    """Test reading back the content of a newly created file."""
    fs, _ = fs_setup
    filename = "READTEST.DOC"
    filedata = b"Some data to read back."
    fs.write_file(filename, filedata)
    _check_fat_mirror(fs)

    read_data = fs.read_file(filename)
    assert read_data == filedata


def test_delete_file_in_root(fs_setup: FSTestFixture) -> None:
    """Test deleting a file and verify its cluster is freed in the FAT."""
    fs, _ = fs_setup
    filename = "TO_DEL.TMP"
    filedata = b"Temporary data."
    fs.write_file(filename, filedata)

    entries = fs.list_directory("/")
    entry = next((e for e in entries if e.name == filename), None)
    assert entry is not None
    start_cluster = entry.starting_cluster
    assert start_cluster is not None and start_cluster > 1

    fat_val_before = _read_test_fat_entry(fs, start_cluster)
    assert fat_val_before != 0

    fs.delete(filename)
    _check_fat_mirror(fs)

    entries_after = fs.list_directory("/")
    assert not any(e.name == filename for e in entries_after)

    fat_val_after = _read_test_fat_entry(fs, start_cluster)
    assert fat_val_after == 0

    fs._cached_allocated_clusters = None
    free_before, _ = fs.get_free_space()
    fs.write_file("DUMMY2.DAT", b"data")
    fs._cached_allocated_clusters = None
    fs.delete("DUMMY2.DAT")
    fs._cached_allocated_clusters = None
    free_after, _ = fs.get_free_space()
    assert free_after >= free_before - fs.allocation_unit_size


def test_create_directory_in_root(fs_setup: FSTestFixture) -> None:
    """Test creating a directory and verify its structure and FAT entry."""
    fs, _ = fs_setup
    dirname = "MYDIR"
    fs.create_directory(dirname)
    _check_fat_mirror(fs)

    entries = fs.list_directory("/")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.name == dirname
    assert entry.is_dir
    assert entry.size == 0
    assert entry.starting_cluster is not None and entry.starting_cluster > 1

    dir_entries = fs._list_directory_by_cluster(entry.starting_cluster)
    dot_entry = next((e for e in dir_entries if e and e.name == "."), None)
    dotdot_entry = next((e for e in dir_entries if e and e.name == ".."), None)

    assert dot_entry is not None
    assert dotdot_entry is not None
    assert dot_entry.starting_cluster == entry.starting_cluster
    assert dotdot_entry.starting_cluster == 0

    fat_val = _read_test_fat_entry(fs, entry.starting_cluster)
    assert fat_val == 0xFFF


def test_create_file_in_subdir(fs_setup: FSTestFixture) -> None:
    """Test writing and reading a file located inside a subdirectory."""
    fs, _ = fs_setup
    dirname = "SUB"
    filename = "INSIDE.DAT"
    filepath = f"{dirname}/{filename}"
    filedata = b"Data inside a subdirectory."

    fs.create_directory(dirname)
    fs.write_file(filepath, filedata)
    _check_fat_mirror(fs)

    sub_entries = fs.list_directory(dirname)
    assert len(sub_entries) == 1
    entry = sub_entries[0]
    assert entry.name == filename
    assert entry.size == len(filedata)
    assert not entry.is_dir

    read_data = fs.read_file(filepath)
    assert read_data == filedata


def test_delete_empty_directory(fs_setup: FSTestFixture) -> None:
    """Test deleting an empty directory and verify its cluster is freed."""
    fs, _ = fs_setup
    dirname = "EMPTYDIR"
    fs.create_directory(dirname)
    dir_entry = next((e for e in fs.list_directory("/") if e.name == dirname), None)
    assert dir_entry is not None
    start_cluster = dir_entry.starting_cluster
    assert start_cluster is not None and start_cluster > 1

    fs.delete(dirname)
    _check_fat_mirror(fs)

    entries_after = fs.list_directory("/")
    assert not any(e.name == dirname for e in entries_after)

    fat_val_after = _read_test_fat_entry(fs, start_cluster)
    assert fat_val_after == 0


def test_delete_non_empty_directory_fails(fs_setup: FSTestFixture) -> None:
    """Test that deleting a non-empty directory raises an OSError."""
    fs, _ = fs_setup
    dirname = "NOTEMPTY"
    filename = "FILE.IN"
    filepath = f"{dirname}/{filename}"
    fs.create_directory(dirname)
    fs.write_file(filepath, b"data")

    with pytest.raises(
        OSError,
        match=f"Could not verify directory contents before deleting: /{dirname}",
    ):
        fs.delete(dirname)

    assert any(e.name == dirname for e in fs.list_directory("/"))
    assert any(e.name == filename for e in fs.list_directory(dirname))


def test_delete_file_in_subdir_then_delete_dir(fs_setup: FSTestFixture) -> None:
    """Test sequential deletion of a file in a subdir, then the subdir."""
    fs, _ = fs_setup
    dirname = "CLEANUP"
    filename = "GONE.TXT"
    filepath = f"{dirname}/{filename}"
    fs.create_directory(dirname)
    fs.write_file(filepath, b"delete me")

    fs.delete(filepath)
    _check_fat_mirror(fs)
    assert not any(e.name == filename for e in fs.list_directory(dirname))

    fs.delete(dirname)
    _check_fat_mirror(fs)
    assert not any(e.name == dirname for e in fs.list_directory("/"))


def test_write_large_file_multiple_clusters(fs_setup: FSTestFixture) -> None:
    """Test writing a file that spans multiple clusters and verify the FAT chain."""
    fs, _ = fs_setup
    filename = "LARGE.BIN"
    filedata = bytes([i % 256 for i in range(1200)])
    assert fs.allocation_unit_size == 512

    fs.write_file(filename, filedata)
    _check_fat_mirror(fs)

    entry = next((e for e in fs.list_directory("/") if e.name == filename), None)
    assert entry is not None
    assert entry.size == len(filedata)

    c1 = entry.starting_cluster
    assert c1 is not None and c1 > 1

    c2 = _read_test_fat_entry(fs, c1)
    assert c2 is not None and c2 >= 2 and c1 != c2

    c3 = _read_test_fat_entry(fs, c2)
    assert c3 is not None and c3 >= 2 and c2 != c3 and c1 != c3

    end_marker = _read_test_fat_entry(fs, c3)
    assert 0xFF8 <= end_marker <= 0xFFF

    read_data = fs.read_file(filename)
    assert read_data == filedata


def test_overwrite_file(fs_setup: FSTestFixture) -> None:
    """Test overwriting an existing file with larger content."""
    fs, _ = fs_setup
    filename = "OVERWRIT.EME"
    initial_data = b"Initial content."
    new_data = b"This new content is much longer and requires more clusters." * 10
    assert len(new_data) > fs.allocation_unit_size > len(initial_data)

    fs.write_file(filename, initial_data)
    entry1 = next((e for e in fs.list_directory("/") if e.name == filename), None)
    assert entry1 and entry1.size == len(initial_data)

    fs.write_file(filename, new_data)
    _check_fat_mirror(fs)

    entry2 = next((e for e in fs.list_directory("/") if e.name == filename), None)
    assert entry2 and entry2.size == len(new_data)

    read_data = fs.read_file(filename)
    assert read_data == new_data


def test_filesystem_info(fs_setup: FSTestFixture) -> None:
    """Test free space and allocation unit tracking."""
    fs, _ = fs_setup
    fs._cached_allocated_clusters = None
    free_start, total_start = fs.get_free_space()
    alloc_start = fs.get_allocated_units()

    expected_data_bytes = fs.num_clusters * fs.allocation_unit_size
    assert total_start == expected_data_bytes
    assert free_start > 0
    assert len(alloc_start) == 0

    filedata = bytes([i % 256 for i in range(1200)])
    fs.write_file("INFO.DAT", filedata)
    fs._cached_allocated_clusters = None

    free_end, total_end = fs.get_free_space()
    alloc_end = fs.get_allocated_units()

    assert total_end == total_start
    assert free_end < free_start
    assert len(alloc_end) == 3
    assert free_start - free_end == 3 * fs.allocation_unit_size


def test_nested_directories(fs_setup: FSTestFixture) -> None:
    """Test creating, writing to, and deleting nested directories."""
    fs, _ = fs_setup
    path = "DIR1/SUBDIR/TARGET.TXT"
    data = b"Deeply nested file."

    fs.create_directory("DIR1")
    fs.create_directory("DIR1/SUBDIR")
    fs.write_file(path, data)
    _check_fat_mirror(fs)

    assert fs.read_file(path) == data

    sub_entries = fs.list_directory("DIR1")
    assert len(sub_entries) == 1 and sub_entries[0].name == "SUBDIR"

    target_entries = fs.list_directory("DIR1/SUBDIR")
    assert len(target_entries) == 1 and target_entries[0].name == "TARGET.TXT"

    fs.delete(path)
    fs.delete("DIR1/SUBDIR")
    fs.delete("DIR1")
    _check_fat_mirror(fs)

    assert fs.list_directory("/") == []


def test_read_file_corrupted_fat_chain_loop(fs_setup: FSTestFixture) -> None:
    """Test reading a file with a FAT chain that contains a loop."""
    fs, disk = fs_setup
    filename = "LOOP.DAT"
    filedata = bytes([i % 256 for i in range(fs.allocation_unit_size * 2 + 10)])
    fs.write_file(filename, filedata)

    entry = next((e for e in fs.list_directory("/") if e.name == filename), None)
    assert entry is not None
    c1 = entry.starting_cluster
    c2 = _read_test_fat_entry(fs, c1)
    c3 = _read_test_fat_entry(fs, c2)
    assert all((c1, c2, c3)) and _read_test_fat_entry(fs, c3) >= 0xFF8

    fs._set_fat_entry_cached(c3, c2)
    fs._commit_fat()
    _check_fat_mirror(fs)

    fs_reloaded = FATFilesystem(disk)
    assert fs_reloaded.get_validity_score() >= fs.VALIDITY_THRESHOLD

    try:
        read_data = fs_reloaded.read_file(filename)
        assert len(read_data) < fs_reloaded.num_clusters * fs_reloaded.allocation_unit_size
    except (IOError, ValueError, IndexError):
        pass


def test_read_file_corrupted_fat_chain_free_sector(fs_setup: FSTestFixture) -> None:
    """Test reading a file where the FAT chain points to a free cluster (0)."""
    fs, disk = fs_setup
    filename = "FREEPTR.DAT"
    filedata = bytes([i % 256 for i in range(fs.allocation_unit_size * 2 + 10)])
    fs.write_file(filename, filedata)

    entry = next((e for e in fs.list_directory("/") if e.name == filename), None)
    assert entry is not None
    c1 = entry.starting_cluster
    c2 = _read_test_fat_entry(fs, c1)
    assert all((c1, c2))

    fs._set_fat_entry_cached(c2, 0)
    fs._commit_fat()
    _check_fat_mirror(fs)

    fs_reloaded = FATFilesystem(disk)
    assert fs_reloaded.get_validity_score() >= fs.VALIDITY_THRESHOLD

    try:
        read_data = fs_reloaded.read_file(filename)
        expected_len = 2 * fs_reloaded.allocation_unit_size
        assert len(read_data) == expected_len
        assert read_data == filedata[:expected_len]
    except Exception as e:
        pytest.fail(f"Unexpected exception reading corrupted chain: {e}")


def test_fat_mirroring_consistency(fs_setup: FSTestFixture) -> None:
    """Test FAT mirroring consistency across a series of FS operations."""
    fs, _ = fs_setup
    fs.write_file("MIRROR1.TXT", b"abc")
    _check_fat_mirror(fs)
    fs.create_directory("MIRRORDR")
    _check_fat_mirror(fs)
    fs.write_file("MIRRORDR/MIRROR2.DAT", b"12345" * 200)
    _check_fat_mirror(fs)
    fs.delete("MIRROR1.TXT")
    _check_fat_mirror(fs)
    fs.delete("MIRRORDR/MIRROR2.DAT")
    _check_fat_mirror(fs)
    fs.delete("MIRRORDR")
    _check_fat_mirror(fs)


def test_write_zero_byte_file(fs_setup: FSTestFixture) -> None:
    """Test that writing a zero-byte file correctly sets the start cluster to 0."""
    fs, _ = fs_setup
    filename = "ZERO.DAT"
    fs.write_file(filename, b"")
    _check_fat_mirror(fs)

    entries = fs.list_directory("/")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.name == filename
    assert entry.size == 0
    assert not entry.is_dir
    assert entry.starting_cluster == 0

    assert fs.read_file(filename) == b""

    fs.delete(filename)
    _check_fat_mirror(fs)
    assert fs.list_directory("/") == []


def test_read_zero_byte_file(fs_setup: FSTestFixture) -> None:
    """Test reading a zero-byte file created by manually writing a dir entry."""
    fs, _ = fs_setup
    filename = "ZEROBYTE.FIL"
    now = datetime.datetime.now()
    entry_data = fs._create_directory_entry_bytes(filename, False, 0, 0, now)
    entry_offset = fs.root_dir_start_offset
    fs._write_bytes(entry_offset, entry_data)

    read_data = fs.read_file(filename)
    assert read_data == b""


def test_init_with_different_geometry_720k(fs_setup: FSTestFixture) -> None:
    """
    Test FS initialization when the disk object has a different geometry.

    The filesystem should always trust the BPB from the boot sector of the
    disk image data, not the geometry set on the Disk object.
    """
    fs, disk = fs_setup
    disk.set_geometry(FMT_720.physical_format)
    fs_reinit = FATFilesystem(disk)
    assert fs_reinit.get_validity_score() >= fs.VALIDITY_THRESHOLD

    assert fs_reinit.boot_sector.total_sectors == 2880
    assert fs_reinit.boot_sector.sectors_per_track == 18
    assert fs_reinit.boot_sector.num_heads == 2

    assert disk.physical_format.total_sectors == FMT_720.physical_format.total_sectors
    assert (
        disk.physical_format.get_sectors_per_track(0, 0)
        == FMT_720.physical_format.get_sectors_per_track(0, 0)
    )


def test_invalid_83_filenames(fs_setup: FSTestFixture) -> None:
    """Test that creating files or directories with invalid 8.3 names fails."""
    fs, _ = fs_setup
    invalid_names = [
        "TOOLONGNAME.TXT",
        "SHORT.TOOLONGEXT",
        "FILE NAME.TXT",
        "FILE?NAME.TXT",
        "COM1.TXT",
        "PRN",
        ".BAD",
        "GOOD.",
        "AUX",
    ]

    for name in invalid_names:
        with pytest.raises(ValueError, match="Invalid 8.3 filename"):
            fs.write_file(name, b"data")

        is_potentially_dir_name = (
            "." not in name
            and not any(c in name for c in r'\\/:*?"<>|')
            and name.upper()
            not in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                "COM1",
                "COM2",
                "COM3",
                "COM4",
                "LPT1",
                "LPT2",
                "LPT3",
            }
            and 0 < len(name) <= 8
        )
        if is_potentially_dir_name:
            with pytest.raises(ValueError, match="Invalid 8.3 directory name"):
                fs.create_directory(name)

    fs.create_directory("GOODDIR")
    assert any(e.name == "GOODDIR" for e in fs.list_directory("/"))


def test_fat_offsets(fs_setup: FSTestFixture) -> None:
    """Verify the calculated offsets for FAT, root dir, and data area."""
    fs, _ = fs_setup
    assert (
        fs.fat_start_offset
        == fs.boot_sector.reserved_sectors * fs.boot_sector.bytes_per_sector
    )
    assert fs.fat_start_offset == 512

    fat_size_bytes = fs.boot_sector.sectors_per_fat * fs.boot_sector.bytes_per_sector
    assert fat_size_bytes == 9 * 512
    assert fat_size_bytes == 4608

    expected_root_start = fs.fat_start_offset + (
        fs.boot_sector.num_fats * fat_size_bytes
    )
    assert fs.root_dir_start_offset == expected_root_start
    assert fs.root_dir_start_offset == 512 + (2 * 4608)
    assert fs.root_dir_start_offset == 9728

    expected_data_start = fs.root_dir_start_offset + (
        fs.boot_sector.root_entries * 32
    )
    assert fs.data_area_start_offset == expected_data_start
    assert fs.data_area_start_offset == 9728 + (224 * 32)
    assert fs.data_area_start_offset == 16896
