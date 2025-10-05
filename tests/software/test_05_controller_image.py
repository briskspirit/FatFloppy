"""
Tests for the DiskController focusing on image file operations.

This module tests the high-level functionality of the DiskController,
which acts as the main public API for interacting with disk images.
"""

import shutil
import sys
from pathlib import Path
from typing import Generator, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.filesystems.fat12_fs import FATFilesystem
from fatfloppy.core.filesystem_registry import FilesystemRegistry

RESOURCE_DIR = Path(__file__).parent.parent / "resources"
POPULATED_IMG_SRC = RESOURCE_DIR / "populated_read_test_144m.img"
EMPTY_IMG_SRC = RESOURCE_DIR / "empty_formatted_144m.img"
TEST_TXT_CONTENT_SRC = RESOURCE_DIR / "TEST.TXT"

_ALL_FORMATS = FilesystemRegistry.get_all_formats()
FMT_144 = _ALL_FORMATS["ibm_3.5_1.44m"]

EmptyControllerFixture = Tuple[DiskController, Path]


@pytest.fixture(scope="function")
def populated_controller(tmp_path: Path) -> Generator[DiskController, None, None]:
    """
    Provides a DiskController instance initialized with a pre-populated disk image.

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

    controller = DiskController()
    success = controller.open_disk(str(test_img_path), disk_type="IMG")
    assert success
    assert controller.disk is not None
    assert controller.filesystem is not None
    assert isinstance(controller.filesystem, FATFilesystem)
    assert (
        controller.filesystem.get_validity_score()
        >= controller.filesystem.VALIDITY_THRESHOLD
    )

    yield controller

    controller.close_disk()


@pytest.fixture(scope="function")
def empty_controller(tmp_path: Path) -> Generator[EmptyControllerFixture, None, None]:
    """
    Provides a DiskController initialized with an empty (formatted) disk image.

    Args:
        tmp_path: The pytest temporary path fixture.

    Yields:
        Tuple containing the initialized DiskController and the path to the
        temporary image file.
    """
    if not EMPTY_IMG_SRC.exists():
        pytest.skip(f"Required resource not found: {EMPTY_IMG_SRC}")

    test_img_path = tmp_path / "empty_ctrl.img"
    shutil.copy(EMPTY_IMG_SRC, test_img_path)

    controller = DiskController()
    success = controller.open_disk(str(test_img_path), disk_type="IMG")
    assert success
    assert controller.disk is not None
    assert controller.filesystem is not None
    assert isinstance(controller.filesystem, FATFilesystem)
    assert (
        controller.filesystem.get_validity_score()
        >= controller.filesystem.VALIDITY_THRESHOLD
    )

    yield controller, test_img_path

    controller.close_disk()


def test_open_image_auto_detect_format(populated_controller: DiskController) -> None:
    """Verify that opening an image correctly auto-detects its format."""
    geom = populated_controller.disk.physical_format
    assert geom is not None

    expected_spt = FMT_144.physical_format.get_sectors_per_track(0, 0)
    actual_spt = geom.get_sectors_per_track(0, 0)
    assert actual_spt == expected_spt
    assert geom.cylinders == FMT_144.physical_format.cylinders
    assert geom.heads == FMT_144.physical_format.heads
    assert geom.bytes_per_sector == FMT_144.physical_format.bytes_per_sector


def test_open_image_non_existent() -> None:
    """Test that attempting to open a non-existent image file fails gracefully."""
    controller = DiskController()
    success = controller.open_disk("/tmp/non_existent_image.img", disk_type="IMG")
    assert not success
    assert controller.disk is None


def test_close_disk(empty_controller: EmptyControllerFixture) -> None:
    """Test that closing a disk properly clears the controller's state."""
    controller, _ = empty_controller
    assert controller.disk is not None
    controller.close_disk()
    assert controller.disk is None
    assert controller.driver is None
    assert controller.filesystem is None


def test_list_directory_root(populated_controller: DiskController) -> None:
    """Test listing the root directory of a populated disk image."""
    entries = populated_controller.list_directory("/")
    assert len(entries) > 0
    found_dir1 = any(e["name"] == "DIR1" and e["is_dir"] for e in entries)
    assert found_dir1


def test_list_subdirectory(populated_controller: DiskController) -> None:
    """Test listing the contents of a subdirectory."""
    entries = populated_controller.list_directory("/DIR1")
    assert len(entries) > 0
    found_subdir = any(e["name"] == "SUBDIR" and e["is_dir"] for e in entries)
    assert found_subdir


def test_read_file(populated_controller: DiskController) -> None:
    """Test reading the contents of a file from the disk image."""
    filepath = "/DIR1/TEST.TXT"
    expected_content = TEST_TXT_CONTENT_SRC.read_bytes()
    read_content = populated_controller.read_file(filepath)
    assert read_content is not None
    assert read_content == expected_content


def test_read_non_existent_file(populated_controller: DiskController) -> None:
    """Test that reading a non-existent file returns None."""
    read_content = populated_controller.read_file("/NO/SUCH/FILE.XYZ")
    assert read_content is None


def test_write_new_file_and_verify(empty_controller: EmptyControllerFixture) -> None:
    """Test writing a new file, closing, reopening, and verifying its content."""
    controller, test_img_path = empty_controller
    filepath = "/NEWFILE.DAT"
    test_content = b"Controller test write data \x00\xff\xfe"
    success = controller.write_file(filepath, test_content)
    assert success

    read_content = controller.read_file(filepath)
    assert read_content == test_content
    controller.close_disk()

    controller_reopened = DiskController()
    reopen_success = controller_reopened.open_disk(str(test_img_path), disk_type="IMG")
    assert reopen_success
    entries = controller_reopened.list_directory("/")
    assert any(e["name"] == "NEWFILE.DAT" for e in entries)
    re_read_content = controller_reopened.read_file(filepath)
    assert re_read_content == test_content
    controller_reopened.close_disk()


def test_create_directory(empty_controller: EmptyControllerFixture) -> None:
    """Test creating nested directories and writing a file inside."""
    controller, _ = empty_controller
    dirpath = "/NEWDIR/SUB"
    success_create1 = controller.create_directory("/NEWDIR")
    assert success_create1
    success_create2 = controller.create_directory(dirpath)
    assert success_create2

    root_list = controller.list_directory("/")
    assert any(e["name"] == "NEWDIR" and e["is_dir"] for e in root_list)
    newdir_list = controller.list_directory("/NEWDIR")
    assert any(e["name"] == "SUB" and e["is_dir"] for e in newdir_list)

    filepath = f"{dirpath}/TEST_SUB.TXT"
    write_success = controller.write_file(filepath, b"hello")
    assert write_success
    read_back = controller.read_file(filepath)
    assert read_back == b"hello"


def test_delete_file_and_directory(empty_controller: EmptyControllerFixture) -> None:
    """Test the sequential deletion of a file and then its parent directory."""
    controller, _ = empty_controller
    dirpath = "/DELDIR"
    filepath = f"{dirpath}/DELFILE.TMP"

    assert controller.create_directory(dirpath)
    assert controller.write_file(filepath, b"to be deleted")
    assert any(e["name"] == "DELDIR" for e in controller.list_directory("/"))
    assert any(e["name"] == "DELFILE.TMP" for e in controller.list_directory(dirpath))

    del_file_success = controller.delete_item(filepath)
    assert del_file_success
    assert not any(
        e["name"] == "DELFILE.TMP" for e in controller.list_directory(dirpath)
    )

    del_dir_success = controller.delete_item(dirpath)
    assert del_dir_success
    assert not any(e["name"] == "DELDIR" for e in controller.list_directory("/"))


def test_get_free_space(populated_controller: DiskController) -> None:
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


def test_get_allocated_units(populated_controller: DiskController) -> None:
    """Test retrieving the list of allocated clusters from the filesystem."""
    clusters = populated_controller.get_allocated_units()
    assert clusters is not None
    assert isinstance(clusters, list)
    assert len(clusters) > 0
    assert all(isinstance(c, int) for c in clusters)
