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

import pytest
import sys
import shutil
import math
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.filesystems.hdos_fs import HDOSFilesystem
from fatfloppy.core.drivers import IMGImageDriver
from fatfloppy.core.disk import Disk

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
    # This value is calculated by the free-chain traversal
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
    # Total sectors: 400, Data area starts at LBA 2, so 398 data sectors
    free_bytes, total_bytes = hdos_controller.get_free_space()
    assert total_bytes == 101888  # 398 data sectors * 256 bytes/sector
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
    # Total: 400 sectors, Data area starts at LBA 2 → 398 data sectors
    # With SPG=2: 398/2 = 199 groups = 101888 bytes total
    # Reserved: 1 group for directory (512 bytes) + 1 group for GRT (512 bytes)
    # Free: 199 - 2 = 197 groups = 100864 bytes
    free_bytes, total_data_bytes = hdos_controller.get_free_space()
    assert total_data_bytes == 101888, f"Expected 101888 total bytes, got {total_data_bytes}"
    
    # Directory and GRT each occupy one 512-byte group
    expected_free = total_data_bytes - (2 * 512)
    assert free_bytes == pytest.approx(expected_free, rel=0.01), \
        f"Expected {expected_free} free bytes, got {free_bytes}"

    # 4. Test a simple write/read cycle on the newly formatted disk
    test_content = b"This is a test file after formatting an HDOS disk."
    write_success = hdos_controller.write_file("/TEST.TXT", test_content)
    assert write_success, "Failed to write file to newly formatted disk"

    read_content = hdos_controller.read_file("/TEST.TXT")
    # assert read_content == test_content, "Content mismatch on newly formatted disk"
    # In test_hdos_format_and_write, replace the read assertion with:
    # HDOS pads files to sector boundaries (256 bytes)
    # Check that content starts correctly
    assert read_content[:len(test_content)] == test_content, "Content prefix mismatch"
    # Check that remainder is null padding
    assert all(b == 0 for b in read_content[len(test_content):]), "Padding should be nulls"
    # Check sector alignment
    assert len(read_content) % 256 == 0, "Should be sector-aligned"

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
