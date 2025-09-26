# tests/software/test_03_filesystem_pytest.py
"""
Comprehensive tests for the FAT12 filesystem implementation.

This module tests file and directory operations such as creation, reading,
writing, and deletion. It also covers more advanced scenarios including
multi-cluster files, file overwriting, nested directories, and handling of
corrupted FAT chains. Filesystem metadata, free space calculation, and
FAT mirroring are also validated.
"""
import datetime
import shutil
import struct
import sys
from pathlib import Path
from typing import Generator, Optional, Tuple

import pytest

# Add the source directory to the Python path for local imports.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers import IMGImageDriver
from fatfloppy.core.filesystems.fat12fs import FATFilesystem, FileInfo
from fatfloppy.core.format_definitions import FLOPPY_FORMATS

# --- Constants and Type Aliases ---

RESOURCE_DIR = Path(__file__).parent.parent / "resources"
EMPTY_IMG_SRC = RESOURCE_DIR / "empty_formatted_144m.img"
FMT_144 = FLOPPY_FORMATS["ibm_3.5_1.44m"]
FMT_720 = FLOPPY_FORMATS["ibm_3.5_720k"]

# Type alias for the fixture's yielded tuple for cleaner type hinting.
FSTestFixture = Tuple[FATFilesystem, Disk]


# --- Fixtures ---

@pytest.fixture(scope="function")
def fs_setup(tmp_path: Path) -> Generator[FSTestFixture, None, None]:
    """
    Set up a driver, disk, and FATFilesystem with a clean image copy.

    This fixture prepares a fresh 1.44MB disk image for each test function,
    initializes the necessary driver and disk objects, and provides a
    FATFilesystem instance ready for testing. Caches are cleared to ensure
    test isolation.

    Args:
        tmp_path: The pytest temporary path fixture.

    Yields:
        A tuple containing the initialized FATFilesystem and Disk instances.
    """
    test_img_path = tmp_path / "test_fs_1.44mb.img"

    if not EMPTY_IMG_SRC.exists():
        pytest.skip(f"{EMPTY_IMG_SRC} not found, cannot run filesystem tests.")
    shutil.copy(EMPTY_IMG_SRC, test_img_path)

    driver = IMGImageDriver(str(test_img_path))
    disk = Disk(driver)

    # Apply the physical format to both the disk and the driver.
    disk.set_geometry(FMT_144.physical_format)
    driver.set_physical_format(FMT_144.physical_format)

    fs = FATFilesystem(disk)
    # Clear caches to ensure tests start from a clean state.
    fs.fat_cache = None
    fs._cached_allocated_clusters = None

    yield fs, disk

    print(f"\n[Fixture Teardown] Filesystem test image {test_img_path} cleanup.")


# --- Helper Functions ---

def _read_test_fat_entry(fs: FATFilesystem, cluster: int) -> Optional[int]:
    """
    Read a FAT12 entry directly from the disk for verification purposes.

    This is a simplistic helper for tests to bypass high-level abstractions and
    check the raw value of a FAT entry on the disk image.

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
            # Use the filesystem's cache for efficiency if available.
            byte_offset = int(cluster * 1.5)
            if byte_offset + 1 >= len(fs.fat_cache):
                return None
            value = struct.unpack_from("<H", fs.fat_cache, byte_offset)[0]
        else:
            # Fallback to a direct read from the disk.
            value_bytes = fs._read_bytes(fat_offset, 2)
            if len(value_bytes) < 2:
                return None  # Handle short read at end of FAT.
            value = struct.unpack("<H", value_bytes)[0]
    except (IOError, ValueError, struct.error, IndexError) as e:
        print(f"Warning: Error reading test FAT entry for cluster {cluster}: {e}")
        return None

    return value & 0x0FFF if cluster % 2 == 0 else value >> 4


def _check_fat_mirror(fs: FATFilesystem) -> None:
    """
    Verify that the first two FATs on the disk are identical.

    Fails the test with an assertion error if the FATs are not mirrored.

    Args:
        fs: The FATFilesystem instance to check.
    """
    if fs.get_validity_score() < fs.VALIDITY_THRESHOLD or fs.boot_sector.num_fats < 2:
        print("DEBUG: Skipping FAT mirror check (FS invalid or < 2 FATs)")
        return

    fat1_offset = fs.fat_start_offset
    fat_size = fs.boot_sector.sectors_per_fat * fs.boot_sector.bytes_per_sector
    fat2_offset = fat1_offset + fat_size

    try:
        fat1_data = fs._read_bytes(fat1_offset, fat_size)
        fat2_data = fs._read_bytes(fat2_offset, fat_size)
        assert fat1_data == fat2_data, (
            f"FAT tables (size {fat_size}) are not mirrored. "
            f"FAT1 starts @ {fat1_offset}, FAT2 starts @ {fat2_offset}"
        )
        print(
            "DEBUG: FAT mirror check PASSED "
            f"(offset {fat1_offset} vs {fat2_offset}, size {fat_size})"
        )
    except Exception as e:
        pytest.fail(f"Error during FAT mirror check: {e}")


# --- Tests ---

def test_01_initialization_valid(fs_setup: FSTestFixture) -> None:
    """Test that the filesystem initializes correctly on a valid 1.44MB image."""
    fs, _ = fs_setup
    assert fs.get_validity_score() >= fs.VALIDITY_THRESHOLD
    assert fs.allocation_unit_size > 0
    assert fs.num_clusters > 0
    # Check parameters specific to the 1.44MB format.
    assert fs.boot_sector.total_sectors == 2880
    assert fs.allocation_unit_size == 512  # 1 sector/cluster for 1.44MB
    assert fs.num_clusters == 2847  # Known value for 1.44MB FAT12


def test_03_list_root_directory_empty(fs_setup: FSTestFixture) -> None:
    """Test listing the root directory of a freshly formatted image."""
    fs, _ = fs_setup
    entries = fs.list_directory("/")
    assert entries == []


def test_04_create_file_in_root(fs_setup: FSTestFixture) -> None:
    """
    Test creating a simple file in the root directory.

    Verifies that the file entry is created correctly, the FAT entry is marked
    as EOC, and free space calculations are updated. Also checks FAT mirroring.
    """
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
    assert fat_val == 0xFFF, f"Single cluster file FAT entry mismatch: {fat_val}"

    # Test free space calculation change.
    fs._cached_allocated_clusters = None  # Reset cache
    free_before, _ = fs.get_free_space()
    fs.write_file("DUMMY.DAT", b"data")  # Allocate another cluster
    fs._cached_allocated_clusters = None  # Reset cache
    free_after, _ = fs.get_free_space()
    assert free_after < free_before, "Free space should decrease after writing file."
    fs.delete("DUMMY.DAT")


def test_05_read_file_in_root(fs_setup: FSTestFixture) -> None:
    """Test reading back the content of a newly created file."""
    fs, _ = fs_setup
    filename = "READTEST.DOC"
    filedata = b"Some data to read back."
    fs.write_file(filename, filedata)
    _check_fat_mirror(fs)

    read_data = fs.read_file(filename)
    assert read_data == filedata


def test_06_delete_file_in_root(fs_setup: FSTestFixture) -> None:
    """Test deleting a file and verify its cluster is freed in the FAT."""
    fs, _ = fs_setup
    filename = "TO_DEL.TMP"
    filedata = b"Temporary data."
    fs.write_file(filename, filedata)

    entries = fs.list_directory("/")
    entry = next((e for e in entries if e.name == filename), None)
    assert entry is not None, "File to be deleted not found."
    start_cluster = entry.starting_cluster
    assert start_cluster is not None and start_cluster > 1

    fat_val_before = _read_test_fat_entry(fs, start_cluster)
    assert fat_val_before != 0, f"Cluster {start_cluster} should be allocated before delete."

    fs.delete(filename)
    _check_fat_mirror(fs)

    entries_after = fs.list_directory("/")
    assert not any(e.name == filename for e in entries_after)

    fat_val_after = _read_test_fat_entry(fs, start_cluster)
    assert fat_val_after == 0, f"Cluster {start_cluster} not freed after delete."

    # Test free space change after delete.
    fs._cached_allocated_clusters = None
    free_before, _ = fs.get_free_space()
    fs.write_file("DUMMY2.DAT", b"data")
    fs._cached_allocated_clusters = None
    fs.delete("DUMMY2.DAT")
    fs._cached_allocated_clusters = None
    free_after, _ = fs.get_free_space()
    assert free_after >= free_before - fs.allocation_unit_size


def test_07_create_directory_in_root(fs_setup: FSTestFixture) -> None:
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

    assert dot_entry is not None, "'.' entry missing in new directory."
    assert dotdot_entry is not None, "'..' entry missing in new directory."
    assert dot_entry.starting_cluster == entry.starting_cluster
    assert dotdot_entry.starting_cluster == 0  # Parent is root.

    fat_val = _read_test_fat_entry(fs, entry.starting_cluster)
    assert fat_val == 0xFFF, "Directory cluster FAT entry should be EOC."


def test_08_create_file_in_subdir(fs_setup: FSTestFixture) -> None:
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


def test_09_delete_empty_directory(fs_setup: FSTestFixture) -> None:
    """Test deleting an empty directory and verify its cluster is freed."""
    fs, _ = fs_setup
    dirname = "EMPTYDIR"
    fs.create_directory(dirname)
    dir_entry = next((e for e in fs.list_directory("/") if e.name == dirname), None)
    assert dir_entry is not None, f"Directory {dirname} not found after creation."
    start_cluster = dir_entry.starting_cluster
    assert start_cluster is not None and start_cluster > 1

    fs.delete(dirname)
    _check_fat_mirror(fs)

    entries_after = fs.list_directory("/")
    assert not any(e.name == dirname for e in entries_after)

    fat_val_after = _read_test_fat_entry(fs, start_cluster)
    assert fat_val_after == 0, f"Directory cluster {start_cluster} not freed."


def test_10_delete_non_empty_directory_fails(fs_setup: FSTestFixture) -> None:
    """Test that deleting a non-empty directory raises an OSError."""
    fs, _ = fs_setup
    dirname = "NOTEMPTY"
    filename = "FILE.IN"
    filepath = f"{dirname}/{filename}"
    fs.create_directory(dirname)
    fs.write_file(filepath, b"data")

    with pytest.raises(OSError, match=f"Could not verify directory contents before deleting: /{dirname}"):
        fs.delete(dirname)

    # Verify directory and file still exist after failed deletion.
    assert any(e.name == dirname for e in fs.list_directory("/"))
    assert any(e.name == filename for e in fs.list_directory(dirname))


def test_11_delete_file_in_subdir_then_delete_dir(fs_setup: FSTestFixture) -> None:
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


def test_12_write_large_file_multiple_clusters(fs_setup: FSTestFixture) -> None:
    """Test writing a file that spans multiple clusters and verify the FAT chain."""
    fs, _ = fs_setup
    filename = "LARGE.BIN"
    # 1200 bytes requires 3 clusters on a 1.44MB disk (512 bytes/cluster).
    filedata = bytes([i % 256 for i in range(1200)])
    assert fs.allocation_unit_size == 512

    fs.write_file(filename, filedata)
    _check_fat_mirror(fs)

    entry = next((e for e in fs.list_directory("/") if e.name == filename), None)
    assert entry is not None, f"File {filename} not found after write."
    assert entry.size == len(filedata)

    c1 = entry.starting_cluster
    assert c1 is not None and c1 > 1

    c2 = _read_test_fat_entry(fs, c1)
    assert c2 is not None and c2 >= 2 and c1 != c2, "Invalid second cluster."

    c3 = _read_test_fat_entry(fs, c2)
    assert c3 is not None and c3 >= 2 and c2 != c3 and c1 != c3, "Invalid third cluster."

    end_marker = _read_test_fat_entry(fs, c3)
    assert 0xFF8 <= end_marker <= 0xFFF, f"Expected EOC marker, got {end_marker:#x}."

    read_data = fs.read_file(filename)
    assert read_data == filedata


def test_13_overwrite_file(fs_setup: FSTestFixture) -> None:
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


def test_14_filesystem_info(fs_setup: FSTestFixture) -> None:
    """Test free space and allocation unit tracking."""
    fs, _ = fs_setup
    fs._cached_allocated_clusters = None  # Ensure recalculation
    free_start, total_start = fs.get_free_space()
    alloc_start = fs.get_allocated_units()

    expected_data_bytes = fs.num_clusters * fs.allocation_unit_size
    assert total_start == expected_data_bytes
    assert free_start > 0
    assert len(alloc_start) == 0, "Expected 0 allocated clusters on empty disk."

    filedata = bytes([i % 256 for i in range(1200)])  # Needs 3 clusters
    fs.write_file("INFO.DAT", filedata)
    fs._cached_allocated_clusters = None  # Ensure recalculation

    free_end, total_end = fs.get_free_space()
    alloc_end = fs.get_allocated_units()

    assert total_end == total_start
    assert free_end < free_start
    assert len(alloc_end) == 3
    assert free_start - free_end == 3 * fs.allocation_unit_size


def test_15_nested_directories(fs_setup: FSTestFixture) -> None:
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

    # Test nested deletion.
    fs.delete(path)
    fs.delete("DIR1/SUBDIR")
    fs.delete("DIR1")
    _check_fat_mirror(fs)

    assert fs.list_directory("/") == [], "Root should be empty after cleanup."


def test_16_read_file_corrupted_fat_chain_loop(fs_setup: FSTestFixture) -> None:
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

    # Manually create a loop in the FAT: c3 -> c2.
    fs._set_fat_entry_cached(c3, c2)
    fs._commit_fat()
    _check_fat_mirror(fs)

    # Reload filesystem to simulate a fresh read with the corrupted disk.
    fs_reloaded = FATFilesystem(disk)
    assert fs_reloaded.get_validity_score() >= fs.VALIDITY_THRESHOLD

    try:
        read_data = fs_reloaded.read_file(filename)
        # Read should stop after detecting the loop, not run infinitely.
        assert len(read_data) < fs_reloaded.num_clusters * fs_reloaded.allocation_unit_size
        print(f"WARN: Read looped file gracefully, len={len(read_data)}")
    except (IOError, ValueError, IndexError) as e:
        print(f"Caught expected exception from reading looped FAT: {type(e).__name__}: {e}")
    except Exception as e:
        pytest.fail(f"Caught unexpected exception reading looped file: {type(e).__name__}: {e}")


def test_17_read_file_corrupted_fat_chain_free_sector(fs_setup: FSTestFixture) -> None:
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

    # Manually corrupt the FAT: make c2 point to 0 (free cluster).
    fs._set_fat_entry_cached(c2, 0)
    fs._commit_fat()
    _check_fat_mirror(fs)

    # Reload filesystem to read from the corrupted disk.
    fs_reloaded = FATFilesystem(disk)
    assert fs_reloaded.get_validity_score() >= fs.VALIDITY_THRESHOLD

    try:
        read_data = fs_reloaded.read_file(filename)
        # The read should be truncated at the corrupted link.
        expected_len = 2 * fs_reloaded.allocation_unit_size
        assert len(read_data) == expected_len
        assert read_data == filedata[:expected_len]
    except Exception as e:
        pytest.fail(f"Unexpected exception reading corrupted chain: {e}")


def test_18_fat_mirroring_consistency(fs_setup: FSTestFixture) -> None:
    """Test FAT mirroring consistency across a series of FS operations."""
    fs, _ = fs_setup
    fs.write_file("MIRROR1.TXT", b"abc")
    _check_fat_mirror(fs)
    fs.create_directory("MIRRORDR")
    _check_fat_mirror(fs)
    fs.write_file("MIRRORDR/MIRROR2.DAT", b"12345" * 200)  # Multi-cluster
    _check_fat_mirror(fs)
    fs.delete("MIRROR1.TXT")
    _check_fat_mirror(fs)
    fs.delete("MIRRORDR/MIRROR2.DAT")
    _check_fat_mirror(fs)
    fs.delete("MIRRORDR")
    _check_fat_mirror(fs)


def test_19_write_zero_byte_file(fs_setup: FSTestFixture) -> None:
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
    # Per FAT spec, a zero-byte file's starting cluster must be 0.
    assert entry.starting_cluster == 0

    assert fs.read_file(filename) == b"", "Reading zero-byte file should be empty."

    fs.delete(filename)
    _check_fat_mirror(fs)
    assert fs.list_directory("/") == [], "Root should be empty after delete."


def test_20_read_zero_byte_file(fs_setup: FSTestFixture) -> None:
    """Test reading a zero-byte file created by manually writing a dir entry."""
    fs, _ = fs_setup
    filename = "ZEROBYTE.FIL"
    now = datetime.datetime.now()
    # Manually create a directory entry with starting cluster 0.
    # FIX: The keyword argument is 'starting_cluster', not 'start_cluster'.
    entry_data = fs._create_directory_entry_bytes(filename, False, 0, 0, now)
    # Find first free entry slot in root (should be the first one).
    entry_offset = fs.root_dir_start_offset
    fs._write_bytes(entry_offset, entry_data)  # Write directly to simulated disk.

    # Read using the filesystem method.
    read_data = fs.read_file(filename)
    assert read_data == b"", "Reading manually created zero-byte file failed."


def test_21_init_with_different_geometry_720k(fs_setup: FSTestFixture) -> None:
    """
    Test FS initialization when the disk object has a different geometry.

    The filesystem should always trust the BPB from the boot sector of the
    disk image data, not the geometry set on the Disk object.
    """
    fs, disk = fs_setup
    # Change the disk object's geometry to 720k.
    disk.set_geometry(FMT_720.physical_format)
    # Re-initialize the FS on the same disk object (which still holds 1.44MB data).
    fs_reinit = FATFilesystem(disk)
    assert fs_reinit.get_validity_score() >= fs.VALIDITY_THRESHOLD

    # Verify the filesystem used the BPB from the 1.44MB image data.
    assert fs_reinit.boot_sector.total_sectors == 2880
    assert fs_reinit.boot_sector.sectors_per_track == 18
    assert fs_reinit.boot_sector.num_heads == 2

    # Verify the Disk object's geometry is still what we set it to (720k).
    assert disk.physical_format.total_sectors == FMT_720.physical_format.total_sectors
    assert (
        disk.physical_format.get_sectors_per_track(0, 0)
        == FMT_720.physical_format.get_sectors_per_track(0, 0)
    )


def test_22_invalid_83_filenames(fs_setup: FSTestFixture) -> None:
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
        print(f"Testing invalid filename write: {name}")
        with pytest.raises(ValueError, match="Invalid 8.3 filename"):
            fs.write_file(name, b"data")

        # Check if the name could theoretically be a directory name.
        is_potentially_dir_name = (
            "." not in name
            and not any(c in name for c in r'\\/:*?"<>|')
            and name.upper() not in {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "LPT1", "LPT2", "LPT3"}
            and 0 < len(name) <= 8
        )
        if is_potentially_dir_name:
            print(f"Testing invalid directory name create: {name}")
            with pytest.raises(ValueError, match="Invalid 8.3 directory name"):
                fs.create_directory(name)

    # Verify a valid name works for contrast.
    fs.create_directory("GOODDIR")
    assert any(e.name == "GOODDIR" for e in fs.list_directory("/"))


def test_23_fat_offsets(fs_setup: FSTestFixture) -> None:
    """Verify the calculated offsets for FAT, root dir, and data area."""
    fs, _ = fs_setup
    # Verify calculations based on the 1.44MB BPB from the image.
    assert fs.fat_start_offset == fs.boot_sector.reserved_sectors * fs.boot_sector.bytes_per_sector
    assert fs.fat_start_offset == 512

    fat_size_bytes = fs.boot_sector.sectors_per_fat * fs.boot_sector.bytes_per_sector
    assert fat_size_bytes == 9 * 512
    assert fat_size_bytes == 4608

    expected_root_start = fs.fat_start_offset + (fs.boot_sector.num_fats * fat_size_bytes)
    assert fs.root_dir_start_offset == expected_root_start
    assert fs.root_dir_start_offset == 512 + (2 * 4608)
    assert fs.root_dir_start_offset == 9728

    expected_data_start = fs.root_dir_start_offset + (fs.boot_sector.root_entries * 32)
    assert fs.data_area_start_offset == expected_data_start
    assert fs.data_area_start_offset == 9728 + (224 * 32)
    assert fs.data_area_start_offset == 16896
