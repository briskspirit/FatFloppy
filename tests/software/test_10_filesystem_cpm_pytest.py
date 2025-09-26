# tests/software/test_10_filesystem_cpm_pytest.py
import pytest
import sys
import shutil
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.filesystems.cpm_fs import CPMFilesystem
from fatfloppy.core.drivers import IMGImageDriver
from fatfloppy.core.disk import Disk

# --- Constants ---
RESOURCE_DIR = Path(__file__).parent.parent / 'resources'
CPM_RESOURCE_DIR = RESOURCE_DIR / 'CPM'
FAT_IMG_SRC = RESOURCE_DIR / 'empty_formatted_144m.img'

@pytest.fixture(scope="function")
def cpm_controller():
    """Provides a clean DiskController instance for each test."""
    controller = DiskController()
    yield controller
    if controller.disk:
        controller.close_disk()


@pytest.mark.parametrize(
    "image_file, disk_type, expected_format_name, expected_free_space_kb, expected_files",
    [
        (
            "disk1.img", "IMG", "cpm_8_sssd_250k", 35,
            ["2FBIOS24.ASM", "DISKTEST.ASM", "READ.ME", "CPM.COM"]
        ),
        (
            "disk17.imd", "IMD", "cpm_8_ssdd_imsai_mixed", 242,
            ["EBASIC.COM", "DENSITY.ASM", "SOLUSER.ASM", "CPM56.COM"]
        ),
    ]
)
def test_cpm_disk_images_read_and_verify(
    cpm_controller, tmp_path, image_file, disk_type, expected_format_name,
    expected_free_space_kb, expected_files
):
    """
    Tests opening CP/M disk images (IMG and IMD), verifying format detection,
    directory listing, free space, and file content integrity.
    """
    # Setup
    controller = cpm_controller
    img_path = CPM_RESOURCE_DIR / image_file

    # 1. Open disk and check format auto-detection
    success = controller.open_disk(str(img_path), disk_type=disk_type)
    assert success, f"Failed to open {img_path}"
    assert controller.disk is not None
    assert isinstance(controller.filesystem, CPMFilesystem)

    detected_format_name, _ = controller.detect_format()
    assert detected_format_name == expected_format_name

    # Check encoding from the profile (Track 0 should be FM for both)
    profile = controller.get_format_by_name(expected_format_name)
    assert profile is not None
    assert profile.physical_format.track_formats[0].encoding == "FM"

    # 2. Check directory listing, filenames, and user
    dir_listing = controller.list_directory("/")
    assert dir_listing, "Directory listing is empty"

    listed_filenames = {item['name'] for item in dir_listing}
    for fname in expected_files:
        assert fname in listed_filenames, f"{fname} not found in directory listing"
        # Check for invalid characters (valid chars are 7-bit printable)
        assert all(32 <= ord(c) < 127 for c in fname if c not in '.'), f"Filename '{fname}' contains invalid characters"

    # 3. Check free space
    free_bytes, _ = controller.get_free_space()
    free_kb = free_bytes / 1024
    assert free_kb == pytest.approx(expected_free_space_kb, abs=1)  # Allow 1KB tolerance

    # 4. Extract and compare files
    for filename in expected_files:
        # Read from disk image
        cpm_filepath = f"/{filename}"
        content_from_disk = controller.read_file(cpm_filepath)
        assert content_from_disk is not None, f"Failed to read {cpm_filepath} from image"

        # Read from ground-truth file
        ground_truth_path = CPM_RESOURCE_DIR / filename
        content_from_file = ground_truth_path.read_bytes()

        # Normalize line endings for comparison (CP/M uses CR, others might use CRLF or LF)
        content_from_disk_norm = content_from_disk.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
        content_from_file_norm = content_from_file.replace(b'\r\n', b'\n').replace(b'\r', b'\n')

        assert content_from_disk_norm == content_from_file_norm, f"Content mismatch for file {filename}"

        # Save extracted file for manual inspection if needed
        (tmp_path / filename).write_bytes(content_from_disk)

    # 5. Close disk
    controller.close_disk()


@pytest.mark.parametrize(
    "image_file, disk_type, file_to_test",
    [
        ("disk1.img", "IMG", "DISKTEST.ASM"),
        ("disk17.imd", "IMD", "DENSITY.ASM"),
    ]
)
def test_cpm_file_write_delete_and_verify(cpm_controller, tmp_path, image_file, disk_type, file_to_test):
    """
    Tests a full delete -> write -> read -> verify cycle for a file on a CP/M image.
    Operates on a temporary copy of the image to avoid modifying source files.
    """
    # --- 1. Setup ---
    controller = cpm_controller
    original_img_path = CPM_RESOURCE_DIR / image_file
    temp_img_path = tmp_path / image_file
    shutil.copy(original_img_path, temp_img_path)

    ground_truth_path = CPM_RESOURCE_DIR / file_to_test
    ground_truth_content = ground_truth_path.read_bytes()
    assert ground_truth_content, "Ground truth file is empty"

    success = controller.open_disk(str(temp_img_path), disk_type=disk_type)
    assert success, f"Failed to open temporary copy of {image_file}"
    assert isinstance(controller.filesystem, CPMFilesystem)

    # --- 2. Initial State Verification ---
    initial_dir = controller.list_directory("/")
    initial_filenames = {item['name'] for item in initial_dir}
    assert file_to_test in initial_filenames, f"File '{file_to_test}' not found in initial directory"
    
    initial_content = controller.read_file(f"/{file_to_test}")
    assert len(initial_content) == len(ground_truth_content), "Initial logical file size mismatch"

    initial_free_bytes, _ = controller.get_free_space()

    # --- 3. Deletion Test ---
    delete_success = controller.delete_item(f"/{file_to_test}")
    assert delete_success, f"Failed to delete '{file_to_test}'"

    dir_after_delete = controller.list_directory("/")
    filenames_after_delete = {item['name'] for item in dir_after_delete}
    assert file_to_test not in filenames_after_delete, f"File '{file_to_test}' still exists after deletion"

    # CRITICAL FIX: Verify free space increased by the space allocated in BLOCKS, not records.
    block_size = controller.filesystem.allocation_unit_size
    assert block_size > 0
    blocks_used = math.ceil(len(ground_truth_content) / block_size)
    space_freed_on_disk = blocks_used * block_size
    
    free_bytes_after_delete, _ = controller.get_free_space()
    assert free_bytes_after_delete == pytest.approx(initial_free_bytes + space_freed_on_disk)

    # --- 4. Write Test ---
    write_success = controller.write_file(f"/{file_to_test}", ground_truth_content)
    assert write_success, f"Failed to write '{file_to_test}' back to the disk"

    dir_after_write = controller.list_directory("/")
    filenames_after_write = {item['name'] for item in dir_after_write}
    assert file_to_test in filenames_after_write, f"File '{file_to_test}' not found after writing it back"

    # --- 5. Read-Back and Content Verification ---
    read_back_content = controller.read_file(f"/{file_to_test}")
    assert read_back_content is not None, "Failed to read back the newly written file"
    assert read_back_content == ground_truth_content, "Content of read-back file does not match original content"

    # --- 6. Final Filesystem Consistency Check ---
    final_free_bytes, _ = controller.get_free_space()
    assert final_free_bytes == pytest.approx(initial_free_bytes), "Free space did not return to initial value"
    
    controller.close_disk()


def test_cpm_format_and_write(cpm_controller, tmp_path):
    """
    Tests the format_disk functionality for CP/M.
    It formats a blank image and verifies its state, then tests a simple write.
    """
    controller = cpm_controller
    profile_name = "cpm_8_sssd_250k"
    profile = controller.get_format_by_name(profile_name)
    assert profile, f"Could not find profile {profile_name}"

    # 1. Create a blank, unformatted image file
    blank_img_path = tmp_path / "blank.img"
    total_bytes = profile.physical_format.total_bytes
    blank_img_path.write_bytes(b'\x00' * total_bytes)

    # 2. Open the blank disk and format it
    assert controller.open_disk(str(blank_img_path), disk_type="IMG", format_info={"format_name": profile_name})
    format_success = controller.format_disk(profile_name)
    assert format_success, "format_disk command failed"
    
    # 3. Verify the formatted state
    assert isinstance(controller.filesystem, CPMFilesystem), "Filesystem is not CP/M after format"
    assert controller.list_directory("/") == [], "Directory is not empty after format"

    # Check that free space is total space minus reserved directory blocks
    dpb = controller.filesystem.dpb
    dir_blocks = dpb.directory_blocks
    block_size = dpb.block_size
    total_data_bytes = (dpb.dsm + 1) * block_size
    expected_free_bytes = total_data_bytes - (dir_blocks * block_size)

    free_bytes, _ = controller.get_free_space()
    assert free_bytes == expected_free_bytes, "Free space is not correct after format"

    # 4. Test a simple write/read cycle on the newly formatted disk
    test_content = b"This is a test file after formatting."
    write_success = controller.write_file("/TEST.TXT", test_content)
    assert write_success, "Failed to write file to newly formatted disk"

    read_content = controller.read_file("/TEST.TXT")
    assert read_content == test_content, "Content mismatch on newly formatted disk"

    controller.close_disk()


def test_cpm_directory_full_error(cpm_controller, tmp_path):
    """
    Tests that writing a file fails with an IOError when the directory is full.
    """
    controller = cpm_controller
    profile_name = "cpm_8_sssd_250k" # This profile has 64 directory entries (DRM=63)
    profile = controller.get_format_by_name(profile_name)
    
    # Create and format a blank image
    img_path = tmp_path / "dir_full.img"
    img_path.write_bytes(b'\x00' * profile.physical_format.total_bytes)
    assert controller.open_disk(str(img_path), disk_type="IMG", format_info={"format_name": profile_name})
    assert controller.format_disk(profile_name)

    # Fill the directory completely
    dpb = controller.filesystem.dpb
    max_entries = dpb.drm + 1
    for i in range(max_entries):
        filename = f"/FILE{i}.TXT"
        assert controller.write_file(filename, b'small')

    # Verify directory is now full
    assert len(controller.list_directory("/")) == max_entries

    # The next write should fail with an IOError
    with pytest.raises(IOError, match="Directory is full"):
        controller.write_file("/EXTRA.TXT", b"this should fail")

    controller.close_disk()


def test_cpm_disk_full_error(cpm_controller, tmp_path):
    """
    Tests that writing a file fails with an IOError when the data area is full.
    """
    controller = cpm_controller
    profile_name = "cpm_8_sssd_250k"
    profile = controller.get_format_by_name(profile_name)
    
    img_path = tmp_path / "disk_full.img"
    img_path.write_bytes(b'\x00' * profile.physical_format.total_bytes)
    assert controller.open_disk(str(img_path), disk_type="IMG", format_info={"format_name": profile_name})
    assert controller.format_disk(profile_name)

    # Calculate and create data to fill most of the disk
    free_bytes, _ = controller.get_free_space()
    # Leave less than one block of space
    data_to_fill_disk = b'\xAA' * (free_bytes - (profile.filesystem_config.block_size - 1))
    
    assert controller.write_file("/BIGFILE.BIN", data_to_fill_disk)

    # The next write should fail as there are no free blocks
    with pytest.raises(IOError, match="Not enough free space"):
        controller.write_file("/SMALL.BIN", b"no space for this")

    controller.close_disk()


def test_cpm_validity_score(cpm_controller):
    """Tests the get_validity_score method against different disk types."""
    controller = cpm_controller

    # 1. Test against a valid CP/M image
    cpm_img_path = CPM_RESOURCE_DIR / "disk1.img"
    if not cpm_img_path.exists():
        pytest.skip(f"CP/M test resource not found: {cpm_img_path}")

    controller.open_disk(str(cpm_img_path), disk_type="IMG")
    assert isinstance(controller.filesystem, CPMFilesystem)
    assert controller.filesystem.get_validity_score() >= CPMFilesystem.VALIDITY_THRESHOLD
    controller.close_disk()

    # 2. Test against a FAT image (should score low when forced to parse as CP/M)
    if not FAT_IMG_SRC.exists():
        pytest.skip(f"FAT test resource not found: {FAT_IMG_SRC}")

    fat_driver = IMGImageDriver(str(FAT_IMG_SRC))
    fat_disk = Disk(fat_driver)
    # Give it a plausible CP/M geometry and DPB to attempt parsing
    cpm_profile = controller.get_format_by_name("cpm_8_sssd_250k")
    fat_disk.set_geometry(cpm_profile.physical_format)
    setattr(fat_disk.physical_format, '_associated_filesystem_config', cpm_profile.filesystem_config)

    cpm_fs_on_fat_disk = CPMFilesystem(fat_disk)
    # Score should be low because directory entries won't look like CP/M
    assert cpm_fs_on_fat_disk.get_validity_score() < CPMFilesystem.VALIDITY_THRESHOLD

    # 3. Test against garbage data (should score 0)
    garbage_data = b'random garbage data' * 20000  # Make it large enough
    garbage_driver = IMGImageDriver("garbage.img", image_data=garbage_data)
    garbage_disk = Disk(garbage_driver)
    garbage_disk.set_geometry(cpm_profile.physical_format)
    setattr(garbage_disk.physical_format, '_associated_filesystem_config', cpm_profile.filesystem_config)

    cpm_fs_on_garbage = CPMFilesystem(garbage_disk)
    assert cpm_fs_on_garbage.get_validity_score() == 0
