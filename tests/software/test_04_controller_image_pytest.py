# tests/test_04_controller_image_pytest.py
"""
Tests for the DiskController focusing on image file operations.

This module tests the high-level functionality of the DiskController,
which acts as the main public API for interacting with disk images. It covers
opening, closing, reading, writing, and navigating the filesystem of disk
images through the controller.
"""

import shutil
import sys
from pathlib import Path
from typing import Generator, Tuple

import pytest

# Add the source directory to the Python path for local imports.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.filesystems.fat12_fs import FATFilesystem
from fatfloppy.core.filesystem_registry import FilesystemRegistry

# --- Constants and Type Aliases ---

RESOURCE_DIR = Path(__file__).parent.parent / "resources"
POPULATED_IMG_SRC = RESOURCE_DIR / "populated_read_test_144m.img"
EMPTY_IMG_SRC = RESOURCE_DIR / "empty_formatted_144m.img"
TEST_TXT_CONTENT_SRC = RESOURCE_DIR / "TEST.TXT"

# Get format from registry
_ALL_FORMATS = FilesystemRegistry.get_all_formats()
FMT_144 = _ALL_FORMATS["ibm_3.5_1.44m"]

# Type alias for the empty_controller fixture's yielded tuple.
EmptyControllerFixture = Tuple[DiskController, Path]


# --- Fixtures ---

@pytest.fixture(scope="function")
def populated_controller(tmp_path: Path) -> Generator[DiskController, None, None]:
    """
    Provides a DiskController instance initialized with a pre-populated disk image.

    This fixture copies a 1.44MB image containing files and directories to a
    temporary location and opens it with a DiskController. It ensures the
    required resource files exist before running the test.

    Args:
        tmp_path: The pytest temporary path fixture.

    Yields:
        An initialized DiskController instance.
    """
    if not POPULATED_IMG_SRC.exists():
        pytest.skip(f"Required resource not found: {POPULATED_IMG_SRC}")
    if not TEST_TXT_CONTENT_SRC.exists():
        pytest.skip(f"Required resource not found: {TEST_TXT_CONTENT_SRC}")

    test_img_path = tmp_path / "populated_ctrl.img"
    shutil.copy(POPULATED_IMG_SRC, test_img_path)
    print(f"\n[Fixture Setup] Copied populated image to {test_img_path}")

    controller = DiskController()
    print(f"[Fixture Setup] Opening populated image: {test_img_path}")
    success = controller.open_disk(str(test_img_path), disk_type="IMG")
    assert success, f"Failed to open disk image {test_img_path}"
    assert controller.disk is not None
    assert controller.filesystem is not None
    assert isinstance(controller.filesystem, FATFilesystem)
    assert controller.filesystem.get_validity_score() >= controller.filesystem.VALIDITY_THRESHOLD

    yield controller

    print(f"[Fixture Teardown] Closing populated disk image: {test_img_path}")
    controller.close_disk()


@pytest.fixture(scope="function")
def empty_controller(tmp_path: Path) -> Generator[EmptyControllerFixture, None, None]:
    """
    Provides a DiskController initialized with an empty (formatted) disk image.

    This fixture copies a clean, formatted 1.44MB image to a temporary location
    and opens it with a DiskController, making it ready for tests that involve
    writing data.

    Args:
        tmp_path: The pytest temporary path fixture.

    Yields:
        A tuple containing the initialized DiskController and the path to the
        temporary image file.
    """
    if not EMPTY_IMG_SRC.exists():
        pytest.skip(f"Required resource not found: {EMPTY_IMG_SRC}")

    test_img_path = tmp_path / "empty_ctrl.img"
    shutil.copy(EMPTY_IMG_SRC, test_img_path)
    print(f"\n[Fixture Setup] Copied empty image to {test_img_path}")

    controller = DiskController()
    print(f"[Fixture Setup] Opening empty image: {test_img_path}")
    success = controller.open_disk(str(test_img_path), disk_type="IMG")
    assert success, f"Failed to open disk image {test_img_path}"
    assert controller.disk is not None
    assert controller.filesystem is not None
    assert isinstance(controller.filesystem, FATFilesystem)
    assert controller.filesystem.get_validity_score() >= controller.filesystem.VALIDITY_THRESHOLD

    yield controller, test_img_path

    print(f"[Fixture Teardown] Closing empty disk image: {test_img_path}")
    controller.close_disk()


# --- Test Cases ---

def test_01_open_image_auto_detect_format(populated_controller: DiskController) -> None:
    """Verify that opening an image correctly auto-detects its format."""
    geom = populated_controller.disk.physical_format
    assert geom is not None, "Disk geometry not set after opening image"

    expected_spt = FMT_144.physical_format.get_sectors_per_track(0, 0)
    actual_spt = geom.get_sectors_per_track(0, 0)
    assert actual_spt == expected_spt, (
        f"SPT incorrect after image open. Expected {expected_spt}, Got: {actual_spt}"
    )
    assert geom.cylinders == FMT_144.physical_format.cylinders
    assert geom.heads == FMT_144.physical_format.heads
    assert geom.bytes_per_sector == FMT_144.physical_format.bytes_per_sector
    print("test_01_open_image_auto_detect_format: PASSED")


def test_02_open_image_non_existent() -> None:
    """Test that attempting to open a non-existent image file fails gracefully."""
    controller = DiskController()
    success = controller.open_disk("/tmp/non_existent_image.img", disk_type="IMG")
    assert not success
    assert controller.disk is None
    print("test_02_open_image_non_existent: PASSED")


def test_03_close_disk(empty_controller: EmptyControllerFixture) -> None:
    """Test that closing a disk properly clears the controller's state."""
    controller, _ = empty_controller
    assert controller.disk is not None
    controller.close_disk()
    assert controller.disk is None
    assert controller.driver is None
    assert controller.filesystem is None
    print("test_03_close_disk: PASSED")


def test_04_list_directory_root(populated_controller: DiskController) -> None:
    """Test listing the root directory of a populated disk image."""
    entries = populated_controller.list_directory("/")
    assert len(entries) > 0, "Populated image root directory is empty?"
    found_dir1 = any(e["name"] == "DIR1" and e["is_dir"] for e in entries)
    assert found_dir1, "Expected 'DIR1' not found in root"
    print("test_04_list_directory_root: PASSED")


def test_05_list_subdirectory(populated_controller: DiskController) -> None:
    """Test listing the contents of a subdirectory."""
    entries = populated_controller.list_directory("/DIR1")
    assert len(entries) > 0, "Populated image DIR1 directory is empty?"
    found_subdir = any(e["name"] == "SUBDIR" and e["is_dir"] for e in entries)
    assert found_subdir, "Expected 'SUBDIR' not found in DIR1"
    print("test_05_list_subdirectory: PASSED")


def test_06_read_file(populated_controller: DiskController) -> None:
    """Test reading the contents of a file from the disk image."""
    filepath = "/DIR1/TEST.TXT"
    expected_content = TEST_TXT_CONTENT_SRC.read_bytes()
    read_content = populated_controller.read_file(filepath)
    assert read_content is not None
    assert read_content == expected_content
    print("test_06_read_file: PASSED")


def test_07_read_non_existent_file(populated_controller: DiskController) -> None:
    """Test that reading a non-existent file returns None."""
    read_content = populated_controller.read_file("/NO/SUCH/FILE.XYZ")
    assert read_content is None
    print("test_07_read_non_existent_file: PASSED")


def test_08_write_new_file_and_verify(empty_controller: EmptyControllerFixture) -> None:
    """
    Test writing a new file, closing, reopening, and verifying its content.
    """
    controller, test_img_path = empty_controller
    filepath = "/NEWFILE.DAT"
    test_content = b"Controller test write data \x00\xff\xfe"
    success = controller.write_file(filepath, test_content)
    assert success

    # Verify content in the same session
    read_content = controller.read_file(filepath)
    assert read_content == test_content
    controller.close_disk()

    # Reopen and verify persistence
    controller_reopened = DiskController()
    reopen_success = controller_reopened.open_disk(str(test_img_path), disk_type="IMG")
    assert reopen_success
    entries = controller_reopened.list_directory("/")
    assert any(e["name"] == "NEWFILE.DAT" for e in entries)
    re_read_content = controller_reopened.read_file(filepath)
    assert re_read_content == test_content
    controller_reopened.close_disk()
    print("test_08_write_new_file_and_verify: PASSED")


def test_09_create_directory(empty_controller: EmptyControllerFixture) -> None:
    """Test creating nested directories and writing a file inside."""
    controller, _ = empty_controller
    dirpath = "/NEWDIR/SUB"
    success_create1 = controller.create_directory("/NEWDIR")
    assert success_create1
    success_create2 = controller.create_directory(dirpath)
    assert success_create2

    # Verify directory structure
    root_list = controller.list_directory("/")
    assert any(e["name"] == "NEWDIR" and e["is_dir"] for e in root_list)
    newdir_list = controller.list_directory("/NEWDIR")
    assert any(e["name"] == "SUB" and e["is_dir"] for e in newdir_list)

    # Verify file creation inside nested directory
    filepath = f"{dirpath}/TEST_SUB.TXT"
    write_success = controller.write_file(filepath, b"hello")
    assert write_success
    read_back = controller.read_file(filepath)
    assert read_back == b"hello"
    print("test_09_create_directory: PASSED")


def test_10_delete_file_and_directory(empty_controller: EmptyControllerFixture) -> None:
    """Test the sequential deletion of a file and then its parent directory."""
    controller, _ = empty_controller
    dirpath = "/DELDIR"
    filepath = f"{dirpath}/DELFILE.TMP"

    # Create and verify items
    assert controller.create_directory(dirpath)
    assert controller.write_file(filepath, b"to be deleted")
    assert any(e["name"] == "DELDIR" for e in controller.list_directory("/"))
    assert any(e["name"] == "DELFILE.TMP" for e in controller.list_directory(dirpath))

    # Delete file and verify
    del_file_success = controller.delete_item(filepath)
    assert del_file_success
    assert not any(e["name"] == "DELFILE.TMP" for e in controller.list_directory(dirpath))

    # Delete directory and verify
    del_dir_success = controller.delete_item(dirpath)
    assert del_dir_success
    assert not any(e["name"] == "DELDIR" for e in controller.list_directory("/"))
    print("test_10_delete_file_and_directory: PASSED")


def test_11_get_free_space(populated_controller: DiskController) -> None:
    """Test retrieving the free and total space from the filesystem."""
    space_info = populated_controller.get_free_space()
    assert space_info is not None

    free_bytes, total_bytes_from_fs = space_info
    assert populated_controller.filesystem is not None

    expected_data_area_bytes = (
        populated_controller.filesystem.num_clusters
        * populated_controller.filesystem.allocation_unit_size
    )
    assert total_bytes_from_fs == expected_data_area_bytes
    assert free_bytes < total_bytes_from_fs
    assert free_bytes >= 0
    print("test_11_get_free_space: PASSED")


def test_12_get_allocated_units(populated_controller: DiskController) -> None:
    """Test retrieving the list of allocated clusters from the filesystem."""
    clusters = populated_controller.get_allocated_units()
    assert clusters is not None
    assert isinstance(clusters, list)
    assert len(clusters) > 0
    assert all(isinstance(c, int) for c in clusters)
    print("test_12_get_allocated_units: PASSED")
