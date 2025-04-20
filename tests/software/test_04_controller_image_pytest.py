# tests/test_04_controller_image_pytest.py

import pytest
import sys
import shutil
from pathlib import Path

# Ensure src is in path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.format_definitions import FLOPPY_FORMATS
from fatfloppy.core.filesystem import FATFilesystem, FileInfo # For type checking

# --- Constants ---
RESOURCE_DIR = Path(__file__).parent.parent / 'resources'
POPULATED_IMG_SRC = RESOURCE_DIR / 'populated_read_test_144m.img'
EMPTY_IMG_SRC = RESOURCE_DIR / 'empty_formatted_144m.img'
TEST_TXT_CONTENT_SRC = RESOURCE_DIR / 'TEST.TXT' # File expected on populated image
FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']

# --- Fixture ---
@pytest.fixture(scope="function")
def populated_controller(tmp_path):
    """Controller opened with populated image (read tests)."""
    if not POPULATED_IMG_SRC.exists():
        pytest.skip(f"Required resource not found: {POPULATED_IMG_SRC}")
    if not TEST_TXT_CONTENT_SRC.exists():
         pytest.skip(f"Required resource not found: {TEST_TXT_CONTENT_SRC}")

    test_img_path = tmp_path / "populated_ctrl.img"
    shutil.copy(POPULATED_IMG_SRC, test_img_path)
    print(f"\n[Fixture Setup] Copied populated image to {test_img_path}")

    controller = DiskController()
    print(f"[Fixture Setup] Opening populated image: {test_img_path}")
    success = controller.open_disk(str(test_img_path), disk_type="image")
    assert success, f"Failed to open disk image {test_img_path}"
    assert controller.disk is not None
    assert controller.filesystem is not None
    assert isinstance(controller.filesystem, FATFilesystem)
    assert controller.filesystem.is_valid()

    yield controller

    print(f"[Fixture Teardown] Closing populated disk image: {test_img_path}")
    controller.close_disk()

@pytest.fixture(scope="function")
def empty_controller(tmp_path):
    """Controller opened with empty image (write tests)."""
    if not EMPTY_IMG_SRC.exists():
        pytest.skip(f"Required resource not found: {EMPTY_IMG_SRC}")

    test_img_path = tmp_path / "empty_ctrl.img"
    shutil.copy(EMPTY_IMG_SRC, test_img_path)
    print(f"\n[Fixture Setup] Copied empty image to {test_img_path}")

    controller = DiskController()
    print(f"[Fixture Setup] Opening empty image: {test_img_path}")
    success = controller.open_disk(str(test_img_path), disk_type="image")
    assert success, f"Failed to open disk image {test_img_path}"
    assert controller.disk is not None
    assert controller.filesystem is not None
    assert isinstance(controller.filesystem, FATFilesystem)
    assert controller.filesystem.is_valid()

    yield controller, test_img_path # Yield path too for verification after close

    print(f"[Fixture Teardown] Closing empty disk image: {test_img_path}")
    controller.close_disk()


# --- Tests ---

def test_01_open_image_auto_detect_format(populated_controller: DiskController):
    # The fixture itself performs the open and initial assertions
    geom = populated_controller.disk.geometry
    # <<< Add assertion to check SPT immediately after open >>>
    assert geom.sectors_per_track == FMT_144.geometry.sectors_per_track, \
        f"SPT incorrect after image open. Expected 18, Got: {geom.sectors_per_track}"

    assert geom.cylinders == FMT_144.geometry.cylinders
    assert geom.heads == FMT_144.geometry.heads
    assert geom.sector_size == FMT_144.geometry.sector_size
    assert populated_controller.filesystem.fat_type == "FAT12"
    print("test_01_open_image_auto_detect_format: PASSED")

def test_02_open_image_non_existent():
    controller = DiskController()
    success = controller.open_disk("/tmp/non_existent_image.img", disk_type="image")
    assert success is False
    assert controller.disk is None
    print("test_02_open_image_non_existent: PASSED")


def test_03_close_disk(empty_controller):
    controller, _ = empty_controller
    # Fixture opens it, we just close it here
    assert controller.disk is not None
    controller.close_disk()
    assert controller.disk is None
    assert controller.driver is None
    assert controller.filesystem is None
    print("test_03_close_disk: PASSED")


def test_04_list_directory_root(populated_controller: DiskController):
    entries = populated_controller.list_directory("/")
    assert len(entries) > 0, "Populated image root directory is empty?"
    found_dir1 = any(e['name'] == 'DIR1' and e['is_dir'] for e in entries)
    assert found_dir1, "Expected 'DIR1' not found in root"
    print("test_04_list_directory_root: PASSED")

def test_05_list_subdirectory(populated_controller: DiskController):
    entries = populated_controller.list_directory("/DIR1")
    assert len(entries) > 0, "Populated image DIR1 directory is empty?"
    found_subdir = any(e['name'] == 'SUBDIR' and e['is_dir'] for e in entries)
    assert found_subdir, "Expected 'SUBDIR' not found in DIR1"
    print("test_05_list_subdirectory: PASSED")


def test_06_read_file(populated_controller: DiskController):
    filepath = "/DIR1/TEST.TXT" # Adjust path as needed
    expected_content = TEST_TXT_CONTENT_SRC.read_bytes()
    read_content = populated_controller.read_file(filepath)
    assert read_content is not None
    assert read_content == expected_content
    print("test_06_read_file: PASSED")


def test_07_read_non_existent_file(populated_controller: DiskController):
    read_content = populated_controller.read_file("/NO/SUCH/FILE.XYZ")
    assert read_content is None
    print("test_07_read_non_existent_file: PASSED")


def test_08_write_new_file_and_verify(empty_controller):
    controller, test_img_path = empty_controller
    filepath = "/NEWFILE.DAT"
    test_content = b"Controller test write data \x00\xff\xfe"
    success = controller.write_file(filepath, test_content)
    assert success is True

    # Read back immediately
    read_content = controller.read_file(filepath)
    assert read_content == test_content

    # Close (flushes) and reopen
    controller.close_disk()
    controller_reopened = DiskController() # New controller instance
    reopen_success = controller_reopened.open_disk(str(test_img_path), disk_type="image")
    assert reopen_success

    # Verify file exists and content is correct after reopen
    entries = controller_reopened.list_directory("/")
    assert any(e['name'] == 'NEWFILE.DAT' for e in entries)
    re_read_content = controller_reopened.read_file(filepath)
    assert re_read_content == test_content
    controller_reopened.close_disk() # Clean up reopened controller
    print("test_08_write_new_file_and_verify: PASSED")

def test_09_create_directory(empty_controller):
    controller, _ = empty_controller
    dirpath = "/NEWDIR/SUB"
    success_create1 = controller.create_directory("/NEWDIR")
    assert success_create1 is True
    success_create2 = controller.create_directory(dirpath)
    assert success_create2 is True

    root_list = controller.list_directory("/")
    assert any(e['name'] == 'NEWDIR' and e['is_dir'] for e in root_list)
    newdir_list = controller.list_directory("/NEWDIR")
    assert any(e['name'] == 'SUB' and e['is_dir'] for e in newdir_list)

    filepath = f"{dirpath}/TEST_SUB.TXT"
    write_success = controller.write_file(filepath, b"hello")
    assert write_success is True
    read_back = controller.read_file(filepath)
    assert read_back == b"hello"
    print("test_09_create_directory: PASSED")

def test_10_delete_file_and_directory(empty_controller):
    controller, _ = empty_controller
    dirpath = "/DELDIR"
    filepath = f"{dirpath}/DELFILE.TMP"

    assert controller.create_directory(dirpath) is True
    assert controller.write_file(filepath, b"to be deleted") is True

    # Verify existence
    assert any(e['name'] == 'DELDIR' for e in controller.list_directory("/"))
    assert any(e['name'] == 'DELFILE.TMP' for e in controller.list_directory(dirpath))

    # Delete file
    del_file_success = controller.delete_item(filepath)
    assert del_file_success is True
    assert not any(e['name'] == 'DELFILE.TMP' for e in controller.list_directory(dirpath))

    # Delete now-empty directory
    del_dir_success = controller.delete_item(dirpath)
    assert del_dir_success is True
    assert not any(e['name'] == 'DELDIR' for e in controller.list_directory("/"))
    print("test_10_delete_file_and_directory: PASSED")


def test_11_get_free_space(populated_controller: DiskController):
    space_info = populated_controller.get_free_space()
    assert space_info is not None
    free_bytes, total_bytes_from_fs = space_info

    assert populated_controller.filesystem is not None
    expected_data_area_bytes = populated_controller.filesystem.num_clusters * populated_controller.filesystem.cluster_size
    assert total_bytes_from_fs == expected_data_area_bytes
    assert free_bytes < total_bytes_from_fs
    assert free_bytes >= 0
    print("test_11_get_free_space: PASSED")


def test_12_get_allocated_clusters(populated_controller: DiskController):
    clusters = populated_controller.get_allocated_clusters()
    assert clusters is not None
    assert isinstance(clusters, list)
    assert len(clusters) > 0 # Populated image must have allocated clusters
    assert all(isinstance(c, int) for c in clusters)
    print("test_12_get_allocated_clusters: PASSED")
