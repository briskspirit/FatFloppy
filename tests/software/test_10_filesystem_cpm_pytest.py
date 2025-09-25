# tests/software/test_10_filesystem_cpm_pytest.py
import pytest
import sys
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
        cpm_fname = f"U0:{fname}"
        assert cpm_fname in listed_filenames, f"{cpm_fname} not found in directory listing"
        # Check for invalid characters (valid chars are 7-bit printable)
        assert all(32 <= ord(c) < 127 for c in fname if c not in '.'), f"Filename '{fname}' contains invalid characters"

    # 3. Check free space
    free_bytes, _ = controller.get_free_space()
    free_kb = free_bytes / 1024
    assert free_kb == pytest.approx(expected_free_space_kb, abs=1)  # Allow 1KB tolerance

    # 4. Extract and compare files
    for filename in expected_files:
        # Read from disk image
        cpm_filepath = f"/U0:{filename}"
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
