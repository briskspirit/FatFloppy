# tests/software/test_11_filesystem_hdos_pytest.py
"""
Pytest module for testing the HDOSFilesystem through the DiskController.

This module focuses on high-level integration tests that simulate user
actions on HDOS formatted disk images. It covers:
- Reading and verifying the contents of a standard HDOS disk image.
- File operations: deleting, writing, and re-reading to verify integrity.
- Disk formatting and subsequent file operations on a newly formatted disk.
- The validity scoring mechanism to differentiate HDOS disks from other formats.
"""
import hashlib
import shutil
import sys
from pathlib import Path
from typing import Iterator

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers import IMGImageDriver
from fatfloppy.core.filesystems.hdos_fs import (HDOS_RGT_SECTOR_LBA,
                                                 HDOSFilesystem)

# --- Constants ---
RESOURCE_DIR = Path(__file__).parent.parent / 'resources'
HDOS_RESOURCE_DIR = RESOURCE_DIR / 'HDOS'
FAT_IMG_SRC = RESOURCE_DIR / 'empty_formatted_144m.img'


# --- Fixtures ---

@pytest.fixture(scope="function")
def hdos_controller() -> Iterator[DiskController]:
    """
    Provides a clean DiskController instance for each test and handles cleanup.

    Yields:
        A new DiskController instance for testing.
    """
    controller = DiskController()
    yield controller
    if controller.disk:
        controller.close_disk()


# --- Tests ---

def test_hdos_disk_image_read_and_verify(
    hdos_controller: DiskController,
    tmp_path: Path
) -> None:
    """
    Tests opening an HDOS disk image, verifying format detection,
    directory listing, free space, and file content integrity.

    Args:
        hdos_controller: The disk controller fixture.
        tmp_path: The pytest temporary path fixture.
    """
    image_file = "HDOS_2-0_TEST.h8d"
    expected_files = ["BITS.ACM", "DVDIO.ACM", "H47LIB.ACM"]
    expected_file_count = 61
    expected_format_name = "hdos_5.25_100k"
    # This value is calculated by the free-chain traversal; this disk is full.
    expected_free_space_bytes = 0

    # 1. Open disk and check format auto-detection
    img_path = HDOS_RESOURCE_DIR / image_file
    success = hdos_controller.open_disk(str(img_path), disk_type="IMG")
    assert success, f"Failed to open {img_path}"
    assert hdos_controller.disk is not None
    assert isinstance(hdos_controller.filesystem, HDOSFilesystem)

    detected_format_name, _ = hdos_controller.detect_format()
    assert detected_format_name == expected_format_name

    # 2. Check directory listing and filenames
    dir_listing = hdos_controller.list_directory("/")
    assert dir_listing, "Directory listing is empty"
    assert len(dir_listing) == expected_file_count

    listed_filenames = {item['name'] for item in dir_listing}
    for fname in expected_files:
        assert fname in listed_filenames, f"{fname} not found in directory listing"

    # 3. Check free space
    free_bytes, total_bytes = hdos_controller.get_free_space()
    # Total sectors: 400, Data area starts at LBA 2, so 398 data sectors
    # 398 data sectors * 256 bytes/sector = 101888 bytes
    assert total_bytes == 101888
    assert free_bytes == pytest.approx(expected_free_space_bytes, rel=0.01)


def test_hdos_file_write_delete_and_verify(
    hdos_controller: DiskController,
    tmp_path: Path
) -> None:
    """
    Tests a full delete -> write -> read -> verify cycle for a file on an HDOS image.
    Operates on a temporary copy of the image to avoid modifying source files.

    Args:
        hdos_controller: The disk controller fixture.
        tmp_path: The pytest temporary path fixture.
    """
    # --- 1. Setup ---
    image_file = "HDOS_2-0_TEST.h8d"
    file_to_test = "DVDIO.ACM"

    original_img_path = HDOS_RESOURCE_DIR / image_file
    temp_img_path = tmp_path / image_file
    shutil.copy(original_img_path, temp_img_path)

    ground_truth_path = HDOS_RESOURCE_DIR / file_to_test
    ground_truth_content = ground_truth_path.read_bytes()
    assert ground_truth_content, "Ground truth file is empty"

    success = hdos_controller.open_disk(str(temp_img_path), disk_type="IMG")
    assert success, f"Failed to open temporary copy of {image_file}"
    assert isinstance(hdos_controller.filesystem, HDOSFilesystem)

    # --- 2. Initial State Verification ---
    initial_dir = hdos_controller.list_directory("/")
    initial_filenames = {item['name'] for item in initial_dir}
    assert file_to_test in initial_filenames, f"File '{file_to_test}' not found in initial directory"
    initial_free_bytes, _ = hdos_controller.get_free_space()
    assert initial_free_bytes == pytest.approx(0, abs=100)  # Allow small tolerance

    # --- 3. Deletion Test ---
    delete_success = hdos_controller.delete_item_recursive(f"/{file_to_test}")
    assert delete_success, f"Failed to delete '{file_to_test}'"

    dir_after_delete = hdos_controller.list_directory("/")
    filenames_after_delete = {item['name'] for item in dir_after_delete}
    assert file_to_test not in filenames_after_delete, f"File '{file_to_test}' still exists after deletion"

    # Verify free space increased.
    # DVDIO.ACM size = 3526 bytes. SPG = 2 (512 bytes per group).
    # Groups needed: ceil(3526/512) = 7 groups = 3584 bytes on disk
    space_freed_on_disk = 7 * 512  # 3584 bytes
    free_bytes_after_delete, _ = hdos_controller.get_free_space()
    # The freed space equals the file's groups since initial was 0
    assert free_bytes_after_delete == pytest.approx(space_freed_on_disk, rel=0.01)

    # --- 4. Write Test ---
    write_success = hdos_controller.write_file(f"/{file_to_test}", ground_truth_content)
    assert write_success, f"Failed to write '{file_to_test}' back to the disk"

    dir_after_write = hdos_controller.list_directory("/")
    filenames_after_write = {item['name'] for item in dir_after_write}
    assert file_to_test in filenames_after_write, f"File '{file_to_test}' not found after writing it back"

    # --- 5. Read-Back and Content Verification ---
    read_back_content = hdos_controller.read_file(f"/{file_to_test}")
    assert read_back_content is not None, "Failed to read back the newly written file"
    assert read_back_content == ground_truth_content, "Content of read-back file does not match original content"

    # --- 6. Final Filesystem Consistency Check ---
    final_free_bytes, _ = hdos_controller.get_free_space()
    assert final_free_bytes == pytest.approx(initial_free_bytes, rel=0.01), "Free space did not return to initial value"

    hdos_controller.close_disk()


def test_hdos_format_and_write(hdos_controller: DiskController, tmp_path: Path) -> None:
    """
    Tests the format_fs functionality for HDOS.

    This test formats a blank image, verifies its clean state, and then performs
    a simple write/read cycle to confirm basic filesystem operations work.

    Args:
        hdos_controller: The disk controller fixture.
        tmp_path: The pytest temporary path fixture.
    """
    profile_name = "hdos_5.25_100k"
    profile = hdos_controller.get_format_by_name(profile_name)
    assert profile, f"Could not find profile {profile_name}"

    # 1. Create a blank, unformatted image file
    blank_img_path = tmp_path / "blank.h8d"
    total_bytes = profile.physical_format.total_bytes
    blank_img_path.write_bytes(b'\x00' * total_bytes)

    # 2. Open the blank disk and format it
    assert hdos_controller.open_disk(str(blank_img_path), disk_type="IMG", format_info={"format_name": profile_name})
    format_success = hdos_controller.format_disk(profile_name)
    assert format_success, "format_disk command failed"

    # 3. Verify the formatted state
    assert isinstance(hdos_controller.filesystem, HDOSFilesystem), "Filesystem is not HDOS after format"
    assert hdos_controller.list_directory("/") == [], "Directory is not empty after format"

    # Check free space
    # Total: 400 sectors, Data area starts at LBA 2 -> 398 data sectors
    # With SPG=2: 398/2 = 199 groups = 101888 bytes total data area
    # Reserved: 4 Track 0 groups + 1 dir group + 1 GRT group = 6 groups = 3072 bytes
    # Free: 199 - 6 = 193 groups = 98816 bytes
    free_bytes, total_data_bytes = hdos_controller.get_free_space()
    expected_total_data = 101888
    expected_free = 193 * 512  # 98816 bytes

    assert total_data_bytes == expected_total_data, f"Expected {expected_total_data} total bytes, got {total_data_bytes}"
    assert free_bytes == pytest.approx(expected_free, rel=0.01), \
        f"Expected {expected_free} free bytes, got {free_bytes}"

    # 4. Test a simple write/read cycle on the newly formatted disk
    test_content = b"This is a test file after formatting an HDOS disk."
    write_success = hdos_controller.write_file("/TEST.TXT", test_content)
    assert write_success, "Failed to write file to newly formatted disk"

    read_content = hdos_controller.read_file("/TEST.TXT")
    # Text files should have trailing nulls trimmed
    assert read_content == test_content, "Content mismatch on newly formatted disk"

    hdos_controller.close_disk()


def test_hdos_validity_score(hdos_controller: DiskController, tmp_path: Path) -> None:
    """
    Tests the get_validity_score method against different disk types.

    Args:
        hdos_controller: The disk controller fixture.
        tmp_path: The pytest temporary path fixture.
    """
    # 1. Test against a valid HDOS image
    hdos_img_path = HDOS_RESOURCE_DIR / "HDOS_2-0_TEST.h8d"
    if not hdos_img_path.exists():
        pytest.skip(f"HDOS test resource not found: {hdos_img_path}")

    hdos_controller.open_disk(str(hdos_img_path), disk_type="IMG")
    assert isinstance(hdos_controller.filesystem, HDOSFilesystem)
    assert hdos_controller.filesystem.get_validity_score() >= HDOSFilesystem.VALIDITY_THRESHOLD
    hdos_controller.close_disk()

    # 2. Test against a FAT image (should score low when parsed as HDOS)
    if not FAT_IMG_SRC.exists():
        pytest.skip(f"FAT test resource not found: {FAT_IMG_SRC}")

    # Create a temporary copy to avoid size mismatch warnings with wrong geometry
    temp_fat_path = tmp_path / "temp_fat.img"
    temp_fat_path.write_bytes(FAT_IMG_SRC.read_bytes()[:102400])

    fat_driver = IMGImageDriver(str(temp_fat_path))
    fat_disk = Disk(fat_driver)

    # Force HDOS geometry onto the FAT disk to attempt parsing
    hdos_profile = hdos_controller.get_format_by_name("hdos_5.25_100k")
    assert hdos_profile is not None
    fat_disk.set_geometry(hdos_profile.physical_format)

    hdos_fs_on_fat_disk = HDOSFilesystem(fat_disk)
    assert hdos_fs_on_fat_disk.get_validity_score() < HDOSFilesystem.VALIDITY_THRESHOLD

    # 3. Test against garbage data (should score 0)
    garbage_data = b'random garbage data' * 5000
    garbage_driver = IMGImageDriver("garbage.img", image_data=garbage_data)
    garbage_disk = Disk(garbage_driver)
    garbage_disk.set_geometry(hdos_profile.physical_format)

    hdos_fs_on_garbage = HDOSFilesystem(garbage_disk)
    assert hdos_fs_on_garbage.get_validity_score() == 0


def test_hdos_system_file_protection(hdos_controller: DiskController, tmp_path: Path) -> None:
    """
    Tests that system files cannot be deleted.

    Note: RGT.SYS, GRT.SYS, DIRECT.SYS are system structures at fixed locations,
    not regular files. This test checks if any files with these names exist in the
    directory and verifies they're protected from deletion.

    Args:
        hdos_controller: The disk controller fixture.
        tmp_path: The pytest temporary path fixture.
    """
    image_file = "HDOS_2-0_TEST.h8d"
    temp_img_path = tmp_path / image_file
    shutil.copy(HDOS_RESOURCE_DIR / image_file, temp_img_path)

    assert hdos_controller.open_disk(str(temp_img_path), disk_type="IMG")

    # Get list of all files
    dir_listing = hdos_controller.list_directory("/")
    filenames = {item['name'] for item in dir_listing}

    # Test protection for any system files that exist as directory entries
    system_files_present = {'RGT.SYS', 'GRT.SYS', 'DIRECT.SYS', 'HDOS.SYS'} & filenames

    for sys_file in system_files_present:
        # Attempt to delete should fail
        with pytest.raises(IOError, match="Cannot delete system file"):
            hdos_controller.filesystem.delete(f"/{sys_file}")

    # Verify filesystem is still intact after failed deletion attempts
    dir_listing_after = hdos_controller.list_directory("/")
    assert len(dir_listing_after) == len(dir_listing), "Directory should be unchanged"

    hdos_controller.close_disk()


def test_hdos_format_creates_structures(hdos_controller: DiskController, tmp_path: Path) -> None:
    """
    Tests that formatting creates all necessary HDOS structures at correct locations.

    Args:
        hdos_controller: The disk controller fixture.
        tmp_path: The pytest temporary path fixture.
    """
    profile_name = "hdos_5.25_100k"
    profile = hdos_controller.get_format_by_name(profile_name)
    assert profile

    blank_img_path = tmp_path / "format_test.h8d"
    blank_img_path.write_bytes(b'\x00' * profile.physical_format.total_bytes)

    assert hdos_controller.open_disk(str(blank_img_path), disk_type="IMG",
                                     format_info={"format_name": profile_name})
    assert hdos_controller.format_disk(profile_name)

    fs = hdos_controller.filesystem
    assert isinstance(fs, HDOSFilesystem)

    # Verify label sector exists and is valid
    assert fs.label is not None
    assert fs.label.volume_number == 0
    assert fs.label.cluster_factor == 2

    # Verify GRT exists at correct location
    assert fs.label.grt_start_block > 0
    grt_data = fs._read_lba(fs.label.grt_start_block)
    assert len(grt_data) == 256
    assert grt_data[0] != 0  # GRT[0] should point to start of free chain

    # Verify RGT exists at LBA 10
    rgt_data = fs._read_lba(HDOS_RGT_SECTOR_LBA)
    assert len(rgt_data) == 256

    # Verify directory exists and is empty
    assert fs.label.dir_start_block > 0
    dir_entries = hdos_controller.list_directory("/")
    assert dir_entries == []

    hdos_controller.close_disk()


def test_hdos_rgt_lockout_functionality(hdos_controller: DiskController, tmp_path: Path) -> None:
    """
    Tests that groups locked out in the RGT are correctly identified and
    excluded from the free chain after formatting.

    Args:
        hdos_controller: The disk controller fixture.
        tmp_path: The pytest temporary path fixture.
    """
    profile_name = "hdos_5.25_100k"
    profile = hdos_controller.get_format_by_name(profile_name)
    assert profile

    blank_img_path = tmp_path / "rgt_test.h8d"
    blank_img_path.write_bytes(b'\x00' * profile.physical_format.total_bytes)

    assert hdos_controller.open_disk(str(blank_img_path), disk_type="IMG",
                                     format_info={"format_name": profile_name})
    assert hdos_controller.format_disk(profile_name)

    fs = hdos_controller.filesystem

    # Verify Track 0 groups are locked out in RGT
    if fs._rgt:
        # Groups 1-4 correspond to the data area on Track 0 and should be locked out
        for group in range(1, 5):
            assert fs._is_group_locked_out(group), f"Group {group} should be locked out"

    # Verify locked-out groups are included in the allocated list
    allocated = fs.get_allocated_units()

    # Groups 1-4 should be in allocated list because they're locked out
    for group in range(1, 5):
        assert group in allocated, f"Locked-out group {group} should be marked allocated"

    hdos_controller.close_disk()


def test_hdos_write_to_full_disk(hdos_controller: DiskController, tmp_path: Path) -> None:
    """
    Tests behavior when attempting to write to a full or nearly-full disk.

    Args:
        hdos_controller: The disk controller fixture.
        tmp_path: The pytest temporary path fixture.
    """
    image_file = "HDOS_2-0_TEST.h8d"
    temp_img_path = tmp_path / image_file
    shutil.copy(HDOS_RESOURCE_DIR / image_file, temp_img_path)

    assert hdos_controller.open_disk(str(temp_img_path), disk_type="IMG")

    # Get current free space (should be 0 on this disk)
    free_bytes, _ = hdos_controller.get_free_space()

    # Try to write a file larger than available space
    large_data = b"X" * (free_bytes + 1024)

    with pytest.raises(IOError, match="Not enough free space"):
        hdos_controller.write_file("/TOOLARGE.TXT", large_data)

    # Filesystem should still be valid after failed write
    dir_after = hdos_controller.list_directory("/")
    assert dir_after is not None

    hdos_controller.close_disk()


def test_hdos_empty_file_operations(hdos_controller: DiskController, tmp_path: Path) -> None:
    """
    Tests creating, reading, and deleting empty (0-byte) files.

    Args:
        hdos_controller: The disk controller fixture.
        tmp_path: The pytest temporary path fixture.
    """
    profile_name = "hdos_5.25_100k"
    profile = hdos_controller.get_format_by_name(profile_name)
    blank_img_path = tmp_path / "empty_test.h8d"
    blank_img_path.write_bytes(b'\x00' * profile.physical_format.total_bytes)

    assert hdos_controller.open_disk(str(blank_img_path), disk_type="IMG",
                                     format_info={"format_name": profile_name})
    assert hdos_controller.format_disk(profile_name)

    # Write empty file
    assert hdos_controller.write_file("/EMPTY.TXT", b"")

    # Verify it appears in directory
    dir_listing = hdos_controller.list_directory("/")
    assert any(f['name'] == 'EMPTY.TXT' for f in dir_listing)

    # Read it back
    content = hdos_controller.read_file("/EMPTY.TXT")
    assert content == b""

    # Get file info
    empty_file_entry = next(f for f in dir_listing if f['name'] == 'EMPTY.TXT')
    assert empty_file_entry['size'] == 0

    # Delete it
    assert hdos_controller.delete_item_recursive("/EMPTY.TXT")

    # Verify it's gone
    dir_after = hdos_controller.list_directory("/")
    assert not any(f['name'] == 'EMPTY.TXT' for f in dir_after)

    hdos_controller.close_disk()


def test_hdos_multiple_file_operations(hdos_controller: DiskController, tmp_path: Path) -> None:
    """
    Tests multiple sequential file operations to verify free space management.

    Args:
        hdos_controller: The disk controller fixture.
        tmp_path: The pytest temporary path fixture.
    """
    profile_name = "hdos_5.25_100k"
    profile = hdos_controller.get_format_by_name(profile_name)
    blank_img_path = tmp_path / "multi_test.h8d"
    blank_img_path.write_bytes(b'\x00' * profile.physical_format.total_bytes)

    assert hdos_controller.open_disk(str(blank_img_path), disk_type="IMG",
                                     format_info={"format_name": profile_name})
    assert hdos_controller.format_disk(profile_name)

    initial_free, _ = hdos_controller.get_free_space()

    # Write 3 files
    files = {
        'FILE1.TXT': b'Content of file one\n' * 50,    # 1000 bytes -> 2 groups
        'FILE2.TXT': b'Second file content\n' * 100,   # 2000 bytes -> 4 groups
        'FILE3.TXT': b'Third file here\n' * 75,        # 1200 bytes -> 3 groups
    }

    for filename, content in files.items():
        assert hdos_controller.write_file(f"/{filename}", content)

    # Verify all exist
    dir_listing = hdos_controller.list_directory("/")
    assert len(dir_listing) == 3

    # Check free space after writing (should be less than initial)
    free_after_write, _ = hdos_controller.get_free_space()
    assert free_after_write < initial_free

    # Delete FILE2 (should free 4 groups)
    assert hdos_controller.delete_item_recursive("/FILE2.TXT")

    # Verify free space increased (compared to after write)
    free_after_delete, _ = hdos_controller.get_free_space()
    assert free_after_delete > free_after_write

    # Should still have less free space than initial because FILE1 and FILE3 still exist
    assert free_after_delete < initial_free

    # Write a new file into the freed space
    assert hdos_controller.write_file("/FILE4.TXT", b'New file\n' * 50)

    # Verify directory has FILE1, FILE3, FILE4 (not FILE2)
    final_listing = hdos_controller.list_directory("/")
    final_names = {f['name'] for f in final_listing}
    assert final_names == {'FILE1.TXT', 'FILE3.TXT', 'FILE4.TXT'}

    # Verify all files are readable
    for filename in final_names:
        content = hdos_controller.read_file(f"/{filename}")
        assert len(content) > 0

    hdos_controller.close_disk()


def test_hdos_free_chain_rebuild(hdos_controller: DiskController, tmp_path: Path) -> None:
    """
    Tests that the free chain is properly rebuilt when GRT[0] = 0.
    This simulates the condition found in the test disk image where the
    free chain is not explicitly maintained.

    Args:
        hdos_controller: The disk controller fixture.
        tmp_path: The pytest temporary path fixture.
    """
    image_file = "HDOS_2-0_TEST.h8d"
    temp_img_path = tmp_path / image_file
    shutil.copy(HDOS_RESOURCE_DIR / image_file, temp_img_path)

    assert hdos_controller.open_disk(str(temp_img_path), disk_type="IMG")

    fs = hdos_controller.filesystem
    initial_grt_0 = fs._grt[0]

    # On this test disk, GRT[0] should be 0 (no maintained free chain)
    assert initial_grt_0 == 0, "Test assumes initial disk has no free chain"

    # Delete a file - this should trigger free chain rebuild
    assert hdos_controller.delete_item_recursive("/BITS.ACM")

    # Reinitialize to reload GRT from the now-modified disk image
    fs._init_completed = False
    fs._initialize()

    # Now GRT[0] should point to the start of a valid free chain
    assert fs._grt[0] != 0, "Free chain should be built after deletion"

    # Verify free space is now trackable and greater than zero
    free_bytes, _ = hdos_controller.get_free_space()
    assert free_bytes > 0

    # Verify we can write a new file using the newly created free chain
    assert hdos_controller.write_file("/NEWFILE.TXT", b"Test content\n" * 20)

    # Verify the file is readable
    content = hdos_controller.read_file("/NEWFILE.TXT")
    assert b"Test content" in content

    hdos_controller.close_disk()


def test_hdos_text_file_null_trimming(hdos_controller: DiskController, tmp_path: Path) -> None:
    """
    Tests that text files have trailing nulls trimmed upon reading,
    but binary files (with low text ratio) do not.

    Args:
        hdos_controller: The disk controller fixture.
        tmp_path: The pytest temporary path fixture.
    """
    profile_name = "hdos_5.25_100k"
    profile = hdos_controller.get_format_by_name(profile_name)
    blank_img_path = tmp_path / "trim_test.h8d"
    blank_img_path.write_bytes(b'\x00' * profile.physical_format.total_bytes)

    assert hdos_controller.open_disk(str(blank_img_path), disk_type="IMG",
                                     format_info={"format_name": profile_name})
    assert hdos_controller.format_disk(profile_name)

    # 1. Write a text file (will be padded to sector boundary on disk)
    text_content = b"This is a text file"
    assert hdos_controller.write_file("/TEXT.TXT", text_content)

    # Read back - should NOT have trailing nulls
    read_text = hdos_controller.read_file("/TEXT.TXT")
    assert read_text == text_content
    assert read_text[-1] != 0, "Text file should have nulls trimmed"

    # 2. Write a binary file with intentional nulls at the end
    binary_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00"
    assert hdos_controller.write_file("/BINARY.DAT", binary_content)

    # Read back - binary files with <90% text ratio should keep nulls
    read_binary = hdos_controller.read_file("/BINARY.DAT")
    # Binary content will be sector-aligned, so it may have padding.
    # We check that the original content is preserved at the start.
    assert read_binary.startswith(binary_content)

    hdos_controller.close_disk()


def test_hdos_large_file_spanning_many_groups(hdos_controller: DiskController, tmp_path: Path) -> None:
    """
    Tests writing and reading a large file that spans 50+ allocation groups.

    This verifies that the GRT chain handling works correctly for large files
    and that file content integrity is maintained across many groups.

    Args:
        hdos_controller: The disk controller fixture.
        tmp_path: The pytest temporary path fixture.
    """
    profile_name = "hdos_5.25_100k"
    profile = hdos_controller.get_format_by_name(profile_name)
    blank_img_path = tmp_path / "large_file_test.h8d"
    blank_img_path.write_bytes(b'\x00' * profile.physical_format.total_bytes)

    assert hdos_controller.open_disk(str(blank_img_path), disk_type="IMG",
                                     format_info={"format_name": profile_name})
    assert hdos_controller.format_disk(profile_name)

    # SPG=2 means 512 bytes per group.
    # To get 60 groups, we need 60 * 512 = 30,720 bytes.
    groups_needed = 60
    bytes_per_group = 512
    file_size = groups_needed * bytes_per_group

    # Create unique content to verify integrity
    pattern = b"HDOS_LARGE_FILE_TEST_PATTERN_" + bytes(range(256))
    large_content = (pattern * (file_size // len(pattern) + 1))[:file_size]

    initial_free, _ = hdos_controller.get_free_space()
    assert initial_free >= file_size, f"Not enough free space: {initial_free} < {file_size}"

    # Write the large file
    filename = "/LARGEFIL.DAT"
    assert hdos_controller.write_file(filename, large_content)

    # Capture free space after write for later comparison
    free_after_write, _ = hdos_controller.get_free_space()
    assert free_after_write < initial_free

    # Verify it appears in directory with correct size
    dir_listing = hdos_controller.list_directory("/")
    large_file_entry = next((f for f in dir_listing if f['name'] == 'LARGEFIL.DAT'), None)
    assert large_file_entry is not None
    assert large_file_entry['size'] == file_size

    # Read back and verify content integrity
    read_content = hdos_controller.read_file(filename)
    assert len(read_content) == len(large_content)
    assert read_content == large_content

    # Delete the large file and verify space is reclaimed
    assert hdos_controller.delete_item_recursive(filename)
    free_after_delete, _ = hdos_controller.get_free_space()
    assert free_after_delete > free_after_write

    # Verify we can write another file to the reclaimed space
    second_file_content = b"Testing reuse of freed space" * 100
    assert hdos_controller.write_file("/SECOND.TXT", second_file_content)
    read_second = hdos_controller.read_file("/SECOND.TXT")
    assert read_second == second_file_content

    hdos_controller.close_disk()


def test_hdos_filename_edge_cases(hdos_controller: DiskController, tmp_path: Path) -> None:
    """
    Tests filename edge cases including 8.3 limits, case sensitivity,
    and truncation. HDOS follows an 8.3 naming convention and stores
    filenames in uppercase.

    Args:
        hdos_controller: The disk controller fixture.
        tmp_path: The pytest temporary path fixture.
    """
    profile_name = "hdos_5.25_100k"
    profile = hdos_controller.get_format_by_name(profile_name)
    blank_img_path = tmp_path / "filename_test.h8d"
    blank_img_path.write_bytes(b'\x00' * profile.physical_format.total_bytes)

    assert hdos_controller.open_disk(str(blank_img_path), disk_type="IMG",
                                     format_info={"format_name": profile_name})
    assert hdos_controller.format_disk(profile_name)

    test_content = b"Test content for filename tests"

    # Test 1: Maximum length filename (8.3)
    assert hdos_controller.write_file("/MAXNAME8.EXT", test_content)
    assert any(f['name'] == 'MAXNAME8.EXT' for f in hdos_controller.list_directory("/"))

    # Test 2: Short filename
    assert hdos_controller.write_file("/A.B", test_content)
    assert any(f['name'] == 'A.B' for f in hdos_controller.list_directory("/"))

    # Test 3: No extension
    assert hdos_controller.write_file("/NOEXT", test_content)
    assert any(f['name'] == 'NOEXT' for f in hdos_controller.list_directory("/"))

    # Test 4: Case insensitivity and truncation
    assert hdos_controller.write_file("/lowercasefile.longext", test_content)
    # Should be uppercased and truncated to LOWERCAS.LON
    assert any(f['name'] == 'LOWERCAS.LON' for f in hdos_controller.list_directory("/"))

    # Test 5: Reading with different case should work
    read_content = hdos_controller.read_file("/LOWERCAS.LON")
    assert read_content == test_content

    # Test 6: Writing to an existing file should overwrite
    original_count = len(hdos_controller.list_directory("/"))
    modified_content = b"Modified content"
    assert hdos_controller.write_file("/A.B", modified_content)
    assert len(hdos_controller.list_directory("/")) == original_count
    assert hdos_controller.read_file("/A.B") == modified_content

    hdos_controller.close_disk()


def test_hdos_read_only_operations_no_modification(hdos_controller: DiskController, tmp_path: Path) -> None:
    """
    Tests that read-only operations do not modify the disk image.

    This verifies that listing directories, reading files, and checking
    free space are non-destructive operations by hashing the disk image
    before and after performing them.

    Args:
        hdos_controller: The disk controller fixture.
        tmp_path: The pytest temporary path fixture.
    """
    image_file = "HDOS_2-0_TEST.h8d"
    temp_img_path = tmp_path / image_file
    shutil.copy(HDOS_RESOURCE_DIR / image_file, temp_img_path)

    # Calculate initial hash of the entire disk image
    initial_hash = hashlib.sha256(temp_img_path.read_bytes()).hexdigest()

    # Open the disk
    assert hdos_controller.open_disk(str(temp_img_path), disk_type="IMG")
    assert isinstance(hdos_controller.filesystem, HDOSFilesystem)

    # Perform multiple read-only operations
    dir_listing_1 = hdos_controller.list_directory("/")
    dir_listing_2 = hdos_controller.list_directory("/")
    assert dir_listing_1 == dir_listing_2

    free_space_1 = hdos_controller.get_free_space()
    free_space_2 = hdos_controller.get_free_space()
    assert free_space_1 == free_space_2

    content_1 = hdos_controller.read_file("/DVDIO.ACM")
    content_2 = hdos_controller.read_file("/DVDIO.ACM")
    assert content_1 == content_2 and len(content_1) > 0

    _ = hdos_controller.read_file("/BITS.ACM")
    _ = hdos_controller.get_allocated_units()

    # Close the disk (this would flush any accidental writes)
    hdos_controller.close_disk()

    # Calculate final hash and verify no changes occurred
    final_hash = hashlib.sha256(temp_img_path.read_bytes()).hexdigest()

    assert initial_hash == final_hash, \
        f"Disk image was modified by read-only operations!\nInitial: {initial_hash}\nFinal: {final_hash}"

    # Reopen and verify data is still intact
    assert hdos_controller.open_disk(str(temp_img_path), disk_type="IMG")
    final_dir_listing = hdos_controller.list_directory("/")
    assert final_dir_listing == dir_listing_1

    final_content = hdos_controller.read_file("/DVDIO.ACM")
    assert final_content == content_1

    hdos_controller.close_disk()
