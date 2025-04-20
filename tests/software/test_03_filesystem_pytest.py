# tests/software/test_03_filesystem_pytest.py
import pytest
import sys
import shutil
import struct
import datetime
from pathlib import Path

# Ensure src is in path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.drivers import RawImageDriver
from fatfloppy.core.disk import Disk
# Import specific errors if needed for asserts
from fatfloppy.core.filesystem import FATFilesystem, FileInfo, ATTR_VOLUME_ID, ATTR_LONG_NAME
from fatfloppy.core.format_definitions import FLOPPY_FORMATS

# --- Constants ---
RESOURCE_DIR = Path(__file__).parent.parent / 'resources'
EMPTY_IMG_SRC = RESOURCE_DIR / 'empty_formatted_144m.img'
FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']
FMT_720 = FLOPPY_FORMATS['ibm_3.5_720k']

# --- Fixture ---
@pytest.fixture(scope="function")
def fs_setup(tmp_path):
    """Sets up driver, disk, and FATFilesystem with a clean image copy."""
    test_img_path = tmp_path / "test_fs_1.44mb.img"

    if not EMPTY_IMG_SRC.exists():
         pytest.skip(f"{EMPTY_IMG_SRC} not found, cannot run filesystem tests.")
    shutil.copy(EMPTY_IMG_SRC, test_img_path)

    driver = RawImageDriver(str(test_img_path))
    disk = Disk(driver)
    disk.set_geometry(FMT_144.geometry)
    driver.set_physical_format(FMT_144.physical_format) # Ensure driver knows format

    fs = FATFilesystem(disk)
    # Clear caches to ensure tests start fresh
    fs.fat_cache = None
    fs._cached_allocated_clusters = None
    # Force load cache if needed (will be loaded on first access anyway by logic)
    # fs._load_fat_cache()

    # Yield filesystem and disk (disk needed for some checks)
    yield fs, disk

    # Teardown managed by tmp_path
    print(f"\n[Fixture Teardown] Filesystem test image {test_img_path} cleanup.")


# --- Helper to read FAT entry (simplistic for testing) ---
def _read_test_fat_entry(fs: FATFilesystem, cluster: int):
     if not fs.is_valid(): return None
     # Use fat_start_offset from the fs instance
     fat_offset = fs.fat_start_offset + int(cluster * 1.5)
     try:
         value_bytes = fs._read_bytes(fat_offset, 2)
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
def _check_fat_mirror(fs: FATFilesystem):
    if not fs.is_valid() or fs.boot_sector.num_fats < 2:
        print("DEBUG: Skipping FAT mirror check (FS invalid or <2 FATs)")
        return True # No second FAT to check

    # Use calculated offsets from the fs instance
    fat1_offset = fs.fat_start_offset
    fat_size = fs.boot_sector.sectors_per_fat * fs.boot_sector.bytes_per_sector
    fat2_offset = fat1_offset + fat_size

    try:
        fat1_data = fs._read_bytes(fat1_offset, fat_size)
        fat2_data = fs._read_bytes(fat2_offset, fat_size)
        assert fat1_data == fat2_data, \
            f"FAT tables (size {fat_size}) are not mirrored. FAT1 starts @ {fat1_offset}, FAT2 starts @ {fat2_offset}"
        print(f"DEBUG: FAT mirror check PASSED (offset {fat1_offset} vs {fat2_offset}, size {fat_size})")
    except Exception as e:
        pytest.fail(f"Error during FAT mirror check: {e}")

# --- Tests ---

def test_01_initialization_valid(fs_setup):
    fs, _ = fs_setup
    assert fs.is_valid()
    assert fs.fat_type == "FAT12"
    assert fs.cluster_size > 0
    assert fs.num_clusters > 0
    # Check specific 1.44MB params
    assert fs.boot_sector.total_sectors == 2880
    assert fs.cluster_size == 512 # 1 sector/cluster for 1.44
    assert fs.num_clusters == 2847 # Hardcoded known value for 1.44MB

def test_02_initialization_invalid_boot_sig(tmp_path):
    # Cannot use fs_setup fixture directly as it needs a valid image first
    test_img_path = tmp_path / "corrupt_sig.img"
    if not EMPTY_IMG_SRC.exists(): pytest.skip("Base image needed")
    shutil.copy(EMPTY_IMG_SRC, test_img_path)

    # Corrupt boot signature in the image file
    with open(test_img_path, "r+b") as f:
        f.seek(510)
        f.write(b'\x00\x00')

    driver_corrupt = RawImageDriver(str(test_img_path))
    disk_corrupt = Disk(driver_corrupt)
    disk_corrupt.set_geometry(FMT_144.geometry) # Set geometry

    fs_corrupt = FATFilesystem(disk_corrupt) # Initialize FS with corrupted data
    assert fs_corrupt.is_valid() is False, "Filesystem should be invalid with bad boot signature"

def test_03_list_root_directory_empty(fs_setup):
    fs, _ = fs_setup
    entries = fs.list_directory("/")
    assert entries == []

def test_04_create_file_in_root(fs_setup):
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
    assert entry.is_dir is False
    assert entry.starting_cluster is not None
    assert entry.starting_cluster > 1

    fat_val = _read_test_fat_entry(fs, entry.starting_cluster)
    assert fat_val == 0xFFF, f"Single cluster file FAT entry mismatch {fat_val}"

    fs._cached_allocated_clusters = None
    free_before, total = fs.get_free_space()
    fs._cached_allocated_clusters = None
    fs.write_file("DUMMY.DAT", b"data")
    fs._cached_allocated_clusters = None
    free_after, _ = fs.get_free_space()
    assert free_after < free_before, "Free space should decrease"
    fs.delete("DUMMY.DAT")

def test_05_read_file_in_root(fs_setup):
    fs, _ = fs_setup
    filename = "READTEST.DOC"
    filedata = b"Some data to read back."
    fs.write_file(filename, filedata)
    _check_fat_mirror(fs)

    read_data = fs.read_file(filename)
    assert read_data == filedata

def test_06_delete_file_in_root(fs_setup):
    fs, _ = fs_setup
    filename = "TO_DEL.TMP"
    filedata = b"Temporary data."
    fs.write_file(filename, filedata)

    entries = fs.list_directory("/")
    assert any(e.name == filename for e in entries)
    entry = next(e for e in entries if e.name == filename)
    start_cluster = entry.starting_cluster
    assert start_cluster is not None and start_cluster > 1

    fat_val_before = _read_test_fat_entry(fs, start_cluster)
    assert fat_val_before != 0

    fs.delete(filename)
    _check_fat_mirror(fs)

    entries_after = fs.list_directory("/")
    assert not any(e.name == filename for e in entries_after)

    fat_val_after = _read_test_fat_entry(fs, start_cluster)
    assert fat_val_after == 0, f"Cluster {start_cluster} not freed"

    fs._cached_allocated_clusters = None
    free_before, total = fs.get_free_space()
    fs._cached_allocated_clusters = None
    fs.write_file("DUMMY2.DAT", b"data")
    fs._cached_allocated_clusters = None
    fs.delete("DUMMY2.DAT")
    fs._cached_allocated_clusters = None
    free_after, _ = fs.get_free_space()
    assert free_after >= free_before, "Free space should increase or stay same"

def test_07_create_directory_in_root(fs_setup):
    fs, _ = fs_setup
    dirname = "MYDIR"
    fs.create_directory(dirname)
    _check_fat_mirror(fs)

    entries = fs.list_directory("/")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.name == dirname
    assert entry.is_dir is True
    assert entry.size == 0
    assert entry.starting_cluster is not None and entry.starting_cluster > 1

    dir_entries = fs._list_directory_by_cluster(entry.starting_cluster)
    filtered_dir_entries = [e for e in dir_entries if not (e.attributes and ("VOL" in e.attributes or "LFN" in e.attributes))]
    assert len(filtered_dir_entries) == 2, f"Expected . and .., found {filtered_dir_entries}"
    assert filtered_dir_entries[0].name == "."
    assert filtered_dir_entries[0].starting_cluster == entry.starting_cluster
    assert filtered_dir_entries[1].name == ".."
    assert filtered_dir_entries[1].starting_cluster == 0 # Parent is root

    fat_val = _read_test_fat_entry(fs, entry.starting_cluster)
    assert fat_val == 0xFFF

def test_08_create_file_in_subdir(fs_setup):
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
    assert entry.is_dir is False

    read_data = fs.read_file(filepath)
    assert read_data == filedata

def test_09_delete_empty_directory(fs_setup):
    fs, _ = fs_setup
    dirname = "EMPTYDIR"
    fs.create_directory(dirname)
    dir_entry = next(e for e in fs.list_directory("/") if e.name == dirname)
    start_cluster = dir_entry.starting_cluster
    assert start_cluster is not None and start_cluster > 1

    fs.delete(dirname)
    _check_fat_mirror(fs)

    entries_after = fs.list_directory("/")
    assert not any(e.name == dirname for e in entries_after)

    fat_val_after = _read_test_fat_entry(fs, start_cluster)
    assert fat_val_after == 0

def test_10_delete_non_empty_directory_fails(fs_setup):
    fs, _ = fs_setup
    dirname = "NOTEMPTY"
    filename = "FILE.IN"
    filepath = f"{dirname}/{filename}"
    fs.create_directory(dirname)
    fs.write_file(filepath, b"data")

    with pytest.raises(OSError, match="Directory not empty"):
        fs.delete(dirname)

    root_entries = fs.list_directory("/")
    assert any(e.name == dirname for e in root_entries)
    sub_entries = fs.list_directory(dirname)
    assert any(e.name == filename for e in sub_entries)

def test_11_delete_file_in_subdir_then_delete_dir(fs_setup):
    fs, _ = fs_setup
    dirname = "CLEANUP"
    filename = "GONE.TXT"
    filepath = f"{dirname}/{filename}"
    fs.create_directory(dirname)
    fs.write_file(filepath, b"delete me")

    fs.delete(filepath)
    _check_fat_mirror(fs)
    fs.delete(dirname)
    _check_fat_mirror(fs)

    root_entries = fs.list_directory("/")
    assert root_entries == []

def test_12_write_large_file_multiple_clusters(fs_setup):
    fs, _ = fs_setup
    filename = "LARGE.BIN"
    filedata = bytes([i % 256 for i in range(1200)]) # Needs 3 clusters
    assert fs.cluster_size == 512

    fs.write_file(filename, filedata)
    _check_fat_mirror(fs)

    entry = next(e for e in fs.list_directory("/") if e.name == filename)
    assert entry.size == len(filedata)

    c1 = entry.starting_cluster
    assert c1 is not None and c1 > 1

    c2 = _read_test_fat_entry(fs, c1)
    assert c2 is not None and c2 > 1 and c1 != c2

    c3 = _read_test_fat_entry(fs, c2)
    assert c3 is not None and c3 > 1 and c2 != c3 and c1 != c3

    end_marker = _read_test_fat_entry(fs, c3)
    assert end_marker == 0xFFF

    read_data = fs.read_file(filename)
    assert read_data == filedata

def test_13_overwrite_file(fs_setup):
    fs, _ = fs_setup
    filename = "OVERWRIT.EME"
    initial_data = b"Initial content."
    new_data = b"This is the new content, much longer than the first."

    fs.write_file(filename, initial_data)
    entry1 = next(e for e in fs.list_directory("/") if e.name == filename)
    cluster1 = entry1.starting_cluster
    size1 = entry1.size
    assert cluster1 is not None

    fs.write_file(filename, new_data)
    _check_fat_mirror(fs)

    entry2 = next(e for e in fs.list_directory("/") if e.name == filename)
    cluster2 = entry2.starting_cluster
    size2 = entry2.size
    assert cluster2 is not None

    assert size2 == len(new_data)
    assert size1 != size2

    read_data = fs.read_file(filename)
    assert read_data == new_data

def test_14_filesystem_info(fs_setup):
    fs, _ = fs_setup
    fs._cached_allocated_clusters = None
    free_start, total_start = fs.get_free_space()
    alloc_start = fs.get_allocated_clusters()

    expected_data_bytes = fs.num_clusters * fs.cluster_size
    assert total_start == expected_data_bytes
    assert free_start > 0
    assert len(alloc_start) == 0

    filedata = bytes([i % 256 for i in range(1200)]) # Needs 3 clusters
    fs._cached_allocated_clusters = None
    fs.write_file("INFO.DAT", filedata)

    fs._cached_allocated_clusters = None
    free_end, total_end = fs.get_free_space()
    alloc_end = fs.get_allocated_clusters()

    assert total_end == total_start
    assert free_end < free_start
    assert len(alloc_end) == 3, f"Expected 3 alloc clusters, got {len(alloc_end)}"
    assert free_start - free_end == 3 * fs.cluster_size

def test_15_nested_directories(fs_setup):
    fs, _ = fs_setup
    path = "DIR1/SUBDIR/TARGET.TXT"
    data = b"Deeply nested file."

    fs.create_directory("DIR1")
    fs.create_directory("DIR1/SUBDIR")
    fs.write_file(path, data)
    _check_fat_mirror(fs)

    read_data = fs.read_file(path)
    assert read_data == data

    sub_entries = fs.list_directory("DIR1")
    assert len(sub_entries) == 1
    assert sub_entries[0].name == "SUBDIR"
    assert sub_entries[0].is_dir is True

    target_entries = fs.list_directory("DIR1/SUBDIR")
    assert len(target_entries) == 1
    assert target_entries[0].name == "TARGET.TXT"
    assert target_entries[0].is_dir is False

    fs.delete(path)
    fs.delete("DIR1/SUBDIR")
    fs.delete("DIR1")
    _check_fat_mirror(fs)

    root_entries = fs.list_directory("/")
    assert root_entries == []

def test_16_read_file_corrupted_fat_chain_loop(fs_setup):
    fs, disk = fs_setup # Need disk to re-init fs
    filename = "LOOP.DAT"
    filedata = bytes([i % 256 for i in range(1200)])
    fs.write_file(filename, filedata)

    entry = next(e for e in fs.list_directory("/") if e.name == filename)
    c1 = entry.starting_cluster
    c2 = _read_test_fat_entry(fs, c1)
    c3 = _read_test_fat_entry(fs, c2)
    assert c1 and c2 and c3 and _read_test_fat_entry(fs, c3) == 0xFFF

    fs._set_fat_entry_cached(c3, c2) # Create loop c3 -> c2
    fs._commit_fat()
    _check_fat_mirror(fs)

    fs.fat_cache = None # Clear cache
    fs_reloaded = FATFilesystem(disk) # Re-init from disk
    assert fs_reloaded.is_valid()

    try:
        read_data = fs_reloaded.read_file(filename)
        assert len(read_data) < fs_reloaded.num_clusters * fs_reloaded.cluster_size
        print(f"WARN: Read looped file OK, len={len(read_data)}")
        assert len(read_data) >= 2 * fs_reloaded.cluster_size
    except (IOError, ValueError, IndexError) as e:
         print(f"Caught expected exception from reading looped FAT: {e}")
         pass # This is also acceptable

def test_17_read_file_corrupted_fat_chain_free_sector(fs_setup):
    fs, disk = fs_setup
    filename = "FREEPTR.DAT"
    filedata = bytes([i % 256 for i in range(1200)]) # 3 clusters
    fs.write_file(filename, filedata)

    entry = next(e for e in fs.list_directory("/") if e.name == filename)
    c1 = entry.starting_cluster
    c2 = _read_test_fat_entry(fs, c1)
    c3 = _read_test_fat_entry(fs, c2)
    assert c1 and c2 and c3 and _read_test_fat_entry(fs, c3) == 0xFFF

    fs._set_fat_entry_cached(c2, 0) # Corrupt: c2 -> 0 (Free)
    fs._commit_fat()
    _check_fat_mirror(fs)

    print("DEBUG: Re-initializing FS object after corruption")
    fs.fat_cache = None
    fs_reloaded = FATFilesystem(disk)
    assert fs_reloaded.is_valid(), "Filesystem invalid after FAT corruption?"
    fs_reloaded.fat_cache = None
    fs_reloaded._cached_allocated_clusters = None

    try:
        read_data = fs_reloaded.read_file(filename)
        expected_len = 2 * fs_reloaded.cluster_size # Should read c1, c2
        assert len(read_data) == expected_len, "Read should truncate"
        assert read_data == filedata[:expected_len]
    except Exception as e:
         pytest.fail(f"Unexpected exception reading corrupted chain: {e}")

def test_18_fat_mirroring_consistency(fs_setup):
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

def test_19_write_zero_byte_file(fs_setup):
    fs, _ = fs_setup
    filename = "ZERO.DAT"
    fs.write_file(filename, b"")
    _check_fat_mirror(fs)

    entries = fs.list_directory("/")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.name == filename
    assert entry.size == 0
    assert entry.is_dir is False
    assert entry.starting_cluster is not None

    start_cluster_for_zero = entry.starting_cluster
    if start_cluster_for_zero == 0:
         print("DEBUG: Zero-byte file cluster 0.")
    elif start_cluster_for_zero >= 2:
         print(f"DEBUG: Zero-byte file cluster {start_cluster_for_zero}.")
         fat_val = _read_test_fat_entry(fs, start_cluster_for_zero)
         assert fat_val == 0xFFF
    else:
         pytest.fail(f"Invalid cluster {start_cluster_for_zero} for zero-byte file.")

    read_data = fs.read_file(filename)
    assert read_data == b""

    fs.delete(filename)
    _check_fat_mirror(fs)
    assert fs.list_directory("/") == []

    if start_cluster_for_zero >= 2:
        fat_val_after = _read_test_fat_entry(fs, start_cluster_for_zero)
        assert fat_val_after == 0

def test_20_read_zero_byte_file(fs_setup):
    fs, _ = fs_setup
    filename = "ZEROBYTE.FIL"
    now = datetime.datetime.now()
    entry_data = fs._create_directory_entry_bytes(filename, False, 0, 0, now)
    entry_offset = fs.root_dir_start_offset
    fs._write_bytes(entry_offset, entry_data)

    read_data = fs.read_file(filename)
    assert read_data == b""

def test_21_init_with_different_geometry_720k(fs_setup):
    fs, disk = fs_setup
    disk.set_geometry(FMT_720.geometry) # Change geometry on disk
    # Re-init FS using the *same disk object* (which holds the original image data)
    fs_reinit = FATFilesystem(disk)
    assert fs_reinit.is_valid()

    # It should use the BPB from the underlying 1.44MB formatted data
    assert fs_reinit.boot_sector.total_sectors == 2880
    assert fs_reinit.boot_sector.sectors_per_track == 18
    assert fs_reinit.boot_sector.num_heads == 2
    assert fs_reinit.cluster_size == 512

def test_22_invalid_83_filenames(fs_setup):
    fs, _ = fs_setup
    invalid_names = [
        "TOOLONGNAME.TXT", "SHORT.TOOLONGEXT", "FILE NAME.TXT",
        "FILE?NAME.TXT", "COM1.TXT", "PRN", ".BAD", "GOOD.",
    ]
    for name in invalid_names:
        with pytest.raises(ValueError, match="Invalid 8.3"):
            fs.write_file(name, b"data")
        # Only test dir creation if suitable
        if '.' not in name and ' ' not in name and '?' not in name and '*' not in name \
           and name.upper() not in ["COM1", "PRN"] and not name.startswith('.'):
             with pytest.raises(ValueError, match="Invalid 8.3 directory name"):
                 fs.create_directory(name)

def test_23_fat_offsets(fs_setup):
    fs, _ = fs_setup
    assert fs.fat_start_offset == 512
    fat_size_bytes = fs.boot_sector.sectors_per_fat * fs.boot_sector.bytes_per_sector
    expected_root_start = fs.fat_start_offset + (fs.boot_sector.num_fats * fat_size_bytes)
    assert fs.root_dir_start_offset == expected_root_start
    assert fs.root_dir_start_offset == 9728
    assert fat_size_bytes == 4608
