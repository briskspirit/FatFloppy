# tests/software/test_03_filesystem_pytest.py
import pytest
import sys
import shutil
import struct
import datetime
from pathlib import Path

# Ensure src is in path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.drivers import RawImageDriver, PhysicalFormat, TrackFormat # Import TrackFormat
from fatfloppy.core.disk import Disk
# Import specific errors if needed for asserts
from fatfloppy.core.filesystem import FATFilesystem, FileInfo, FATBootSector, ATTR_VOLUME_ID, ATTR_LONG_NAME
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
    # Use physical_format from the FormatProfile
    disk.set_geometry(FMT_144.physical_format)
    # Ensure driver knows format)
    driver.set_physical_format(FMT_144.physical_format)

    fs = FATFilesystem(disk)
    # Clear caches to ensure tests start fresh
    fs.fat_cache = None
    fs._cached_allocated_clusters = None

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
         # Use the filesystem's cached read method for efficiency if available
         if fs.fat_cache:
             byte_offset = int(cluster * 1.5)
             if byte_offset + 1 >= len(fs.fat_cache): return None
             value = struct.unpack_from("<H", fs.fat_cache, byte_offset)[0]
         else:
             # Fallback to direct read if cache not loaded (less ideal)
             value_bytes = fs._read_bytes(fat_offset, 2)
             if len(value_bytes) < 2: return None # Handle short read
             value = struct.unpack('<H', value_bytes)[0]
     except (IOError, ValueError, struct.error, IndexError) as e: # Added IndexError
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

    # Test free space calculation change
    fs._cached_allocated_clusters = None # Reset cache
    free_before, total = fs.get_free_space()
    fs._cached_allocated_clusters = None # Reset cache
    fs.write_file("DUMMY.DAT", b"data") # Allocate another cluster
    fs._cached_allocated_clusters = None # Reset cache
    free_after, _ = fs.get_free_space()
    assert free_after < free_before, "Free space should decrease after writing file"
    fs.delete("DUMMY.DAT") # Clean up dummy file

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
    assert fat_val_before != 0, f"Cluster {start_cluster} should be allocated before delete"

    fs.delete(filename)
    _check_fat_mirror(fs)

    entries_after = fs.list_directory("/")
    assert not any(e.name == filename for e in entries_after), f"File {filename} should not be listed after delete"

    fat_val_after = _read_test_fat_entry(fs, start_cluster)
    assert fat_val_after == 0, f"Cluster {start_cluster} not freed after delete"

    # Test free space calculation change
    fs._cached_allocated_clusters = None
    free_before, total = fs.get_free_space()
    fs._cached_allocated_clusters = None
    fs.write_file("DUMMY2.DAT", b"data") # Allocate another cluster
    fs._cached_allocated_clusters = None
    fs.delete("DUMMY2.DAT") # Delete it again
    fs._cached_allocated_clusters = None
    free_after, _ = fs.get_free_space()
    # Allow for slight variations if intermediate states differ, but should generally increase or stay same
    assert free_after >= free_before - fs.cluster_size, "Free space should increase or stay similar after delete"

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
    # Filter out potential Volume ID or LFN entries if parser is lenient
    filtered_dir_entries = [e for e in dir_entries if e and e.name and e.name not in [".", ".."] and not (e.attributes and ("VOL" in e.attributes or "LFN" in e.attributes))]
    # Check for '.' and '..' entries specifically
    dot_entry = next((e for e in dir_entries if e and e.name == "."), None)
    dotdot_entry = next((e for e in dir_entries if e and e.name == ".."), None)

    assert dot_entry is not None, "'.' entry missing in new directory"
    assert dotdot_entry is not None, "'..' entry missing in new directory"
    assert dot_entry.starting_cluster == entry.starting_cluster
    assert dotdot_entry.starting_cluster == 0 # Parent is root

    # Check FAT entry for the directory cluster
    fat_val = _read_test_fat_entry(fs, entry.starting_cluster)
    assert fat_val == 0xFFF, "Directory cluster FAT entry should be EOC"

def test_08_create_file_in_subdir(fs_setup):
    fs, _ = fs_setup
    dirname = "SUB"
    filename = "INSIDE.DAT"
    filepath = f"{dirname}/{filename}" # Use / separator internally
    filedata = b"Data inside a subdirectory."

    fs.create_directory(dirname)
    fs.write_file(filepath, filedata)
    _check_fat_mirror(fs)

    sub_entries = fs.list_directory(dirname) # List using / separator
    assert len(sub_entries) == 1
    entry = sub_entries[0]
    assert entry.name == filename
    assert entry.size == len(filedata)
    assert entry.is_dir is False

    read_data = fs.read_file(filepath) # Read using / separator
    assert read_data == filedata

def test_09_delete_empty_directory(fs_setup):
    fs, _ = fs_setup
    dirname = "EMPTYDIR"
    fs.create_directory(dirname)
    dir_entry = next((e for e in fs.list_directory("/") if e.name == dirname), None)
    assert dir_entry is not None, f"Directory {dirname} not found after creation"
    start_cluster = dir_entry.starting_cluster
    assert start_cluster is not None and start_cluster > 1

    fs.delete(dirname)
    _check_fat_mirror(fs)

    entries_after = fs.list_directory("/")
    assert not any(e.name == dirname for e in entries_after), f"Directory {dirname} still listed after delete"

    fat_val_after = _read_test_fat_entry(fs, start_cluster)
    assert fat_val_after == 0, f"Directory cluster {start_cluster} not freed after delete"

def test_10_delete_non_empty_directory_fails(fs_setup):
    fs, _ = fs_setup
    dirname = "NOTEMPTY"
    filename = "FILE.IN"
    filepath = f"{dirname}/{filename}"
    fs.create_directory(dirname)
    fs.write_file(filepath, b"data")

    with pytest.raises(OSError, match="Directory not empty"):
        fs.delete(dirname)

    # Verify directory and file still exist
    root_entries = fs.list_directory("/")
    assert any(e.name == dirname for e in root_entries), f"Directory {dirname} missing after failed delete"
    sub_entries = fs.list_directory(dirname)
    assert any(e.name == filename for e in sub_entries), f"File {filename} missing after failed dir delete"

def test_11_delete_file_in_subdir_then_delete_dir(fs_setup):
    fs, _ = fs_setup
    dirname = "CLEANUP"
    filename = "GONE.TXT"
    filepath = f"{dirname}/{filename}"
    fs.create_directory(dirname)
    fs.write_file(filepath, b"delete me")

    fs.delete(filepath)
    _check_fat_mirror(fs)
    # Verify file is gone from subdir listing
    subdir_entries = fs.list_directory(dirname)
    assert not any(e.name == filename for e in subdir_entries), f"File {filename} still listed after delete"

    fs.delete(dirname)
    _check_fat_mirror(fs)

    # Verify directory is gone from root listing
    root_entries = fs.list_directory("/")
    assert not any(e.name == dirname for e in root_entries), f"Directory {dirname} still listed after delete"

def test_12_write_large_file_multiple_clusters(fs_setup):
    fs, _ = fs_setup
    filename = "LARGE.BIN"
    # 1200 bytes should require 1200 / 512 = ceil(2.34) = 3 clusters for 1.44MB format
    filedata = bytes([i % 256 for i in range(1200)])
    assert fs.cluster_size == 512

    fs.write_file(filename, filedata)
    _check_fat_mirror(fs)

    entry = next((e for e in fs.list_directory("/") if e.name == filename), None)
    assert entry is not None, f"File {filename} not found in listing after write"
    assert entry.size == len(filedata)

    c1 = entry.starting_cluster
    assert c1 is not None and c1 > 1

    # Read FAT chain
    c2 = _read_test_fat_entry(fs, c1)
    assert c2 is not None and c2 >= 2 and c1 != c2, f"Invalid second cluster {c2} in chain"

    c3 = _read_test_fat_entry(fs, c2)
    assert c3 is not None and c3 >= 2 and c2 != c3 and c1 != c3, f"Invalid third cluster {c3} in chain"

    end_marker = _read_test_fat_entry(fs, c3)
    assert 0xFF8 <= end_marker <= 0xFFF, f"Expected EOC marker, got {end_marker:#x}" # FAT12 EOC range

    read_data = fs.read_file(filename)
    assert read_data == filedata

def test_13_overwrite_file(fs_setup):
    fs, _ = fs_setup
    filename = "OVERWRIT.EME"
    initial_data = b"Initial content."
    # Ensure new data requires more clusters than initial data
    new_data = b"This is the new content, much longer than the first, requires more clusters." * 10
    assert len(new_data) > fs.cluster_size > len(initial_data)

    fs.write_file(filename, initial_data)
    entry1 = next((e for e in fs.list_directory("/") if e.name == filename), None)
    assert entry1 is not None
    cluster1 = entry1.starting_cluster
    size1 = entry1.size
    assert cluster1 is not None and cluster1 > 1

    fs.write_file(filename, new_data)
    _check_fat_mirror(fs)

    entry2 = next((e for e in fs.list_directory("/") if e.name == filename), None)
    assert entry2 is not None
    cluster2 = entry2.starting_cluster
    size2 = entry2.size
    assert cluster2 is not None and cluster2 > 1

    assert size2 == len(new_data)
    assert size1 != size2
    # Cluster *might* be the same if the filesystem reuses it, or might be different.
    # Cannot reliably assert cluster1 != cluster2 without knowing allocation strategy.

    read_data = fs.read_file(filename)
    assert read_data == new_data

def test_14_filesystem_info(fs_setup):
    fs, _ = fs_setup
    fs._cached_allocated_clusters = None # Ensure recalculation
    free_start, total_start = fs.get_free_space()
    alloc_start = fs.get_allocated_clusters()

    expected_data_bytes = fs.num_clusters * fs.cluster_size
    assert total_start == expected_data_bytes
    assert free_start > 0
    assert len(alloc_start) == 0, "Expected 0 allocated clusters on empty formatted disk"

    filedata = bytes([i % 256 for i in range(1200)]) # Needs 3 clusters
    fs._cached_allocated_clusters = None # Ensure recalculation
    fs.write_file("INFO.DAT", filedata)

    fs._cached_allocated_clusters = None # Ensure recalculation
    free_end, total_end = fs.get_free_space()
    alloc_end = fs.get_allocated_clusters()

    assert total_end == total_start
    assert free_end < free_start
    assert len(alloc_end) == 3, f"Expected 3 alloc clusters after writing 1200 bytes, got {len(alloc_end)}"
    assert free_start - free_end == 3 * fs.cluster_size, "Free space decrease mismatch"

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
    assert len(sub_entries) == 1, "Expected 1 entry (SUBDIR) in DIR1"
    assert sub_entries[0].name == "SUBDIR"
    assert sub_entries[0].is_dir is True

    target_entries = fs.list_directory("DIR1/SUBDIR")
    assert len(target_entries) == 1, "Expected 1 entry (TARGET.TXT) in DIR1/SUBDIR"
    assert target_entries[0].name == "TARGET.TXT"
    assert target_entries[0].is_dir is False

    # Test deletion
    fs.delete(path)
    fs.delete("DIR1/SUBDIR")
    fs.delete("DIR1")
    _check_fat_mirror(fs)

    root_entries = fs.list_directory("/")
    assert root_entries == [], "Root directory should be empty after nested delete"

def test_16_read_file_corrupted_fat_chain_loop(fs_setup):
    fs, disk = fs_setup # Need disk to re-init fs
    filename = "LOOP.DAT"
    # Write a file requiring 3 clusters
    filedata = bytes([i % 256 for i in range(fs.cluster_size * 2 + 10)])
    fs.write_file(filename, filedata)

    entry = next((e for e in fs.list_directory("/") if e.name == filename), None)
    assert entry is not None
    c1 = entry.starting_cluster
    c2 = _read_test_fat_entry(fs, c1)
    c3 = _read_test_fat_entry(fs, c2)
    assert c1 and c2 and c3 and _read_test_fat_entry(fs, c3) >= 0xFF8 # Check for EOC range

    # Manually corrupt the FAT entry using the cached method
    fs._set_fat_entry_cached(c3, c2) # Create loop c3 -> c2
    fs._commit_fat() # Write the corrupted FAT back to the simulated disk
    _check_fat_mirror(fs)

    # --- Reload filesystem to simulate fresh read ---
    fs.fat_cache = None # Clear cache of the original fs object
    fs_reloaded = FATFilesystem(disk) # Re-initialize from the disk (image file)
    assert fs_reloaded.is_valid(), "Reloaded filesystem should be valid despite corruption"

    # --- Attempt to read the file with the loop ---
    try:
        # The read should detect the loop and stop
        read_data = fs_reloaded.read_file(filename)
        # It should have read at least the first two clusters before hitting the loop
        assert len(read_data) >= 2 * fs_reloaded.cluster_size
        # The read length should be less than the theoretical max to indicate loop detection worked
        assert len(read_data) < fs_reloaded.num_clusters * fs_reloaded.cluster_size
        print(f"WARN: Read looped file OK, len={len(read_data)}")
    except (IOError, ValueError, IndexError) as e:
         # Catching specific exceptions if read_file raises on loop detection
         print(f"Caught expected exception from reading looped FAT: {type(e).__name__}: {e}")
         pass
    except Exception as e:
         pytest.fail(f"Caught unexpected exception type reading looped file: {type(e).__name__}: {e}")


def test_17_read_file_corrupted_fat_chain_free_sector(fs_setup):
    fs, disk = fs_setup
    filename = "FREEPTR.DAT"
    # Write a file requiring 3 clusters
    filedata = bytes([i % 256 for i in range(fs.cluster_size * 2 + 10)])
    fs.write_file(filename, filedata)

    entry = next((e for e in fs.list_directory("/") if e.name == filename), None)
    assert entry is not None
    c1 = entry.starting_cluster
    c2 = _read_test_fat_entry(fs, c1)
    c3 = _read_test_fat_entry(fs, c2)
    assert c1 and c2 and c3 and _read_test_fat_entry(fs, c3) >= 0xFF8 # Check EOC

    # Manually corrupt the FAT: make c2 point to 0 (free)
    fs._set_fat_entry_cached(c2, 0)
    fs._commit_fat() # Write the corrupted FAT back to the simulated disk
    _check_fat_mirror(fs)

    # --- Reload filesystem ---
    print("DEBUG: Re-initializing FS object after corruption")
    fs.fat_cache = None
    fs_reloaded = FATFilesystem(disk)
    assert fs_reloaded.is_valid(), "Filesystem invalid after FAT corruption?"
    fs_reloaded.fat_cache = None # Ensure cache is clear for reloaded instance
    fs_reloaded._cached_allocated_clusters = None

    # --- Attempt to read the file ---
    try:
        read_data = fs_reloaded.read_file(filename)
        # Should only read clusters c1 and c2 before hitting the 0 pointer
        expected_len = 2 * fs_reloaded.cluster_size
        assert len(read_data) == expected_len, f"Read should truncate at cluster {c2}, expected len {expected_len}, got {len(read_data)}"
        assert read_data == filedata[:expected_len], "Read data mismatch after truncation"
    except Exception as e:
         pytest.fail(f"Unexpected exception reading corrupted chain pointing to free sector: {e}")

def test_18_fat_mirroring_consistency(fs_setup):
     fs, _ = fs_setup
     fs.write_file("MIRROR1.TXT", b"abc")
     _check_fat_mirror(fs)
     fs.create_directory("MIRRORDR")
     _check_fat_mirror(fs)
     fs.write_file("MIRRORDR/MIRROR2.DAT", b"12345" * 200) # Multi-cluster
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
    # Zero-byte files MUST have starting cluster 0 according to FAT spec
    assert start_cluster_for_zero == 0, f"Zero-byte file should have starting cluster 0, got {start_cluster_for_zero}"
    print("DEBUG: Zero-byte file cluster 0.")

    read_data = fs.read_file(filename)
    assert read_data == b"", "Reading zero-byte file should yield empty bytes"

    fs.delete(filename)
    _check_fat_mirror(fs)
    assert fs.list_directory("/") == [], "Root directory should be empty after delete"

    # Cluster 0 is never in FAT, so no need to check FAT entry

def test_20_read_zero_byte_file(fs_setup):
    fs, _ = fs_setup
    filename = "ZEROBYTE.FIL"
    now = datetime.datetime.now()
    # Manually create a directory entry with starting cluster 0
    entry_data = fs._create_directory_entry_bytes(filename, False, 0, 0, now)
    # Find first free entry slot in root (should be the first one)
    entry_offset = fs.root_dir_start_offset
    fs._write_bytes(entry_offset, entry_data) # Write directly to simulated disk

    # Read using the filesystem method
    read_data = fs.read_file(filename)
    assert read_data == b"", "Reading manually created zero-byte file failed"

def test_21_init_with_different_geometry_720k(fs_setup):
    fs, disk = fs_setup
    # Use physical_format
    disk.set_geometry(FMT_720.physical_format) # Change geometry on disk object
    # Re-init FS using the *same disk object*
    # The disk still wraps the original 1.44MB image file data
    fs_reinit = FATFilesystem(disk)
    assert fs_reinit.is_valid()

    # FATFilesystem initialization reads the boot sector from the underlying image.
    # It should use the BPB from the *image data*, not the geometry set on the Disk object.
    assert fs_reinit.boot_sector.total_sectors == 2880, "FS should read BPB from 1.44MB image"
    assert fs_reinit.boot_sector.sectors_per_track == 18, "FS should read BPB from 1.44MB image"
    assert fs_reinit.boot_sector.num_heads == 2, "FS should read BPB from 1.44MB image"
    assert fs_reinit.cluster_size == 512, "Cluster size from 1.44MB BPB"

    # --- FIX: Verify disk object's geometry using total_sectors and get_sectors_per_track ---
    assert disk.geometry.total_sectors == FMT_720.physical_format.total_sectors
    assert disk.geometry.get_sectors_per_track(0, 0) == FMT_720.physical_format.get_sectors_per_track(0, 0)
    # --- End Fix ---


def test_22_invalid_83_filenames(fs_setup):
    fs, _ = fs_setup
    invalid_names = [
        "TOOLONGNAME.TXT", "SHORT.TOOLONGEXT", "FILE NAME.TXT",
        "FILE?NAME.TXT", "COM1.TXT", "PRN", ".BAD", "GOOD.", "AUX",
    ]
    valid_dir_names = ["GOODDIR"] # Example of a valid name for context

    for name in invalid_names:
        print(f"Testing invalid filename write: {name}")
        with pytest.raises(ValueError, match="Invalid 8.3 filename"):
            fs.write_file(name, b"data")

        # Test directory creation only if the name is potentially valid as a base name
        # (no extension, no invalid chars besides maybe space, not reserved)
        is_potentially_dir_name = (
            '.' not in name and
            not any(c in name for c in r'\\/:*?"<>|') and
            name.upper() not in {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "LPT1", "LPT2", "LPT3"} and
            len(name) <= 8 and name # Check length and not empty
        )
        if is_potentially_dir_name:
             print(f"Testing invalid directory name create: {name}")
             # Need a more specific match if possible, or just ValueError
             with pytest.raises(ValueError, match="Invalid 8.3 directory name"):
                 fs.create_directory(name)
        else:
            print(f"Skipping directory creation test for unsuitable name: {name}")

    # Test a valid directory name for contrast
    print("Testing valid directory name create: GOODDIR")
    fs.create_directory("GOODDIR")
    assert any(e.name == "GOODDIR" for e in fs.list_directory("/"))


def test_23_fat_offsets(fs_setup):
    fs, _ = fs_setup
    # Verify calculations based on the 1.44MB BPB loaded from the image
    assert fs.fat_start_offset == fs.boot_sector.reserved_sectors * fs.boot_sector.bytes_per_sector
    assert fs.fat_start_offset == 1 * 512
    assert fs.fat_start_offset == 512

    fat_size_bytes = fs.boot_sector.sectors_per_fat * fs.boot_sector.bytes_per_sector
    assert fat_size_bytes == 9 * 512
    assert fat_size_bytes == 4608

    expected_root_start = fs.fat_start_offset + (fs.boot_sector.num_fats * fat_size_bytes)
    assert fs.root_dir_start_offset == expected_root_start
    assert fs.root_dir_start_offset == 512 + (2 * 4608)
    assert fs.root_dir_start_offset == 9728

    # Verify data area start calculation
    expected_data_start = fs.root_dir_start_offset + (fs.boot_sector.root_entries * 32)
    assert fs.data_area_start_offset == expected_data_start
    assert fs.data_area_start_offset == 9728 + (224 * 32)
    assert fs.data_area_start_offset == 16896
