"""
Pytest module for testing the HDOSFilesystem through the DiskController.

This module focuses on high-level integration tests that simulate user
actions on HDOS formatted disk images.
"""

import hashlib
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers import IMGImageDriver
from fatfloppy.core.filesystems.hdos_fs import HDOS_RGT_SECTOR_LBA, HDOSFilesystem

RESOURCE_DIR = Path(__file__).parent.parent / "resources"
HDOS_RESOURCE_DIR = RESOURCE_DIR / "HDOS"
FAT_IMG_SRC = RESOURCE_DIR / "empty_formatted_144m.img"


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


def test_disk_image_read_and_verify(hdos_controller: DiskController) -> None:
    """
    Tests opening an HDOS disk image, verifying format detection,
    directory listing, free space, and file content integrity.
    """
    image_file = "HDOS_2-0_TEST.h8d"
    expected_files = ["BITS.ACM", "DVDIO.ACM", "H47LIB.ACM"]
    expected_file_count = 61
    expected_format_name = "hdos_5.25_100k"
    expected_free_space_bytes = 0

    img_path = HDOS_RESOURCE_DIR / image_file
    success = hdos_controller.open_disk(str(img_path), disk_type="IMG")
    assert success
    assert hdos_controller.disk is not None
    assert isinstance(hdos_controller.filesystem, HDOSFilesystem)

    detected_format_name, _, _ = hdos_controller.detect_format()
    assert detected_format_name == expected_format_name

    dir_listing = hdos_controller.list_directory("/")
    assert dir_listing
    assert len(dir_listing) == expected_file_count

    listed_filenames = {item["name"] for item in dir_listing}
    for fname in expected_files:
        assert fname in listed_filenames

    free_bytes, total_bytes = hdos_controller.get_free_space()
    assert total_bytes == 101888
    assert free_bytes == pytest.approx(expected_free_space_bytes, rel=0.01)


def test_file_write_delete_and_verify(
    hdos_controller: DiskController, tmp_path: Path
) -> None:
    """Tests a full delete -> write -> read -> verify cycle for a file on an HDOS image."""
    image_file = "HDOS_2-0_TEST.h8d"
    file_to_test = "DVDIO.ACM"

    original_img_path = HDOS_RESOURCE_DIR / image_file
    temp_img_path = tmp_path / image_file
    shutil.copy(original_img_path, temp_img_path)

    ground_truth_path = HDOS_RESOURCE_DIR / file_to_test
    ground_truth_content = ground_truth_path.read_bytes()
    assert ground_truth_content

    success = hdos_controller.open_disk(str(temp_img_path), disk_type="IMG")
    assert success
    assert isinstance(hdos_controller.filesystem, HDOSFilesystem)

    initial_dir = hdos_controller.list_directory("/")
    initial_filenames = {item["name"] for item in initial_dir}
    assert file_to_test in initial_filenames
    initial_free_bytes, _ = hdos_controller.get_free_space()
    assert initial_free_bytes == pytest.approx(0, abs=100)

    delete_success = hdos_controller.delete_item_recursive(f"/{file_to_test}")
    assert delete_success

    dir_after_delete = hdos_controller.list_directory("/")
    filenames_after_delete = {item["name"] for item in dir_after_delete}
    assert file_to_test not in filenames_after_delete

    space_freed_on_disk = 7 * 512
    free_bytes_after_delete, _ = hdos_controller.get_free_space()
    assert free_bytes_after_delete == pytest.approx(space_freed_on_disk, rel=0.01)

    write_success = hdos_controller.write_file(f"/{file_to_test}", ground_truth_content)
    assert write_success

    dir_after_write = hdos_controller.list_directory("/")
    filenames_after_write = {item["name"] for item in dir_after_write}
    assert file_to_test in filenames_after_write

    read_back_content = hdos_controller.read_file(f"/{file_to_test}")
    assert read_back_content is not None
    assert read_back_content == ground_truth_content

    final_free_bytes, _ = hdos_controller.get_free_space()
    assert final_free_bytes == pytest.approx(initial_free_bytes, rel=0.01)

    hdos_controller.close_disk()


def test_format_and_write(hdos_controller: DiskController, tmp_path: Path) -> None:
    """Tests the format_fs functionality for HDOS."""
    profile_name = "hdos_5.25_100k"
    profile = hdos_controller.get_format_by_name(profile_name)
    assert profile

    blank_img_path = tmp_path / "blank.h8d"
    total_bytes = profile.physical_format.total_bytes
    blank_img_path.write_bytes(b"\x00" * total_bytes)

    assert hdos_controller.open_disk(
        str(blank_img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert hdos_controller.format_disk_media(profile_name)
    format_success = hdos_controller.format_disk_media(profile_name)
    assert format_success

    assert isinstance(hdos_controller.filesystem, HDOSFilesystem)
    assert hdos_controller.list_directory("/") == []

    free_bytes, total_data_bytes = hdos_controller.get_free_space()
    expected_total_data = 101888
    expected_free = 193 * 512

    assert total_data_bytes == expected_total_data
    assert free_bytes == pytest.approx(expected_free, rel=0.01)

    test_content = b"This is a test file after formatting an HDOS disk."
    write_success = hdos_controller.write_file("/TEST.TXT", test_content)
    assert write_success

    read_content = hdos_controller.read_file("/TEST.TXT")
    assert read_content == test_content

    hdos_controller.close_disk()


def test_validity_score(hdos_controller: DiskController, tmp_path: Path) -> None:
    """Tests the get_validity_score method against different disk types."""
    hdos_img_path = HDOS_RESOURCE_DIR / "HDOS_2-0_TEST.h8d"
    if not hdos_img_path.exists():
        pytest.skip(f"HDOS test resource not found: {hdos_img_path}")

    hdos_controller.open_disk(str(hdos_img_path), disk_type="IMG")
    assert isinstance(hdos_controller.filesystem, HDOSFilesystem)
    assert (
        hdos_controller.filesystem.get_validity_score()
        >= HDOSFilesystem.VALIDITY_THRESHOLD
    )
    hdos_controller.close_disk()

    if not FAT_IMG_SRC.exists():
        pytest.skip(f"FAT test resource not found: {FAT_IMG_SRC}")

    temp_fat_path = tmp_path / "temp_fat.img"
    temp_fat_path.write_bytes(FAT_IMG_SRC.read_bytes()[:102400])

    fat_driver = IMGImageDriver(str(temp_fat_path))
    fat_disk = Disk(fat_driver)

    hdos_profile = hdos_controller.get_format_by_name("hdos_5.25_100k")
    assert hdos_profile is not None
    fat_disk.set_geometry(hdos_profile.physical_format)

    hdos_fs_on_fat_disk = HDOSFilesystem(fat_disk)
    assert hdos_fs_on_fat_disk.get_validity_score() < HDOSFilesystem.VALIDITY_THRESHOLD

    garbage_data = b"random garbage data" * 5000
    garbage_driver = IMGImageDriver("garbage.img", image_data=garbage_data)
    garbage_disk = Disk(garbage_driver)
    garbage_disk.set_geometry(hdos_profile.physical_format)

    hdos_fs_on_garbage = HDOSFilesystem(garbage_disk)
    assert hdos_fs_on_garbage.get_validity_score() == 0


def test_system_file_protection(
    hdos_controller: DiskController, tmp_path: Path
) -> None:
    """Tests that system files cannot be deleted."""
    image_file = "HDOS_2-0_TEST.h8d"
    temp_img_path = tmp_path / image_file
    shutil.copy(HDOS_RESOURCE_DIR / image_file, temp_img_path)

    assert hdos_controller.open_disk(str(temp_img_path), disk_type="IMG")

    dir_listing = hdos_controller.list_directory("/")
    filenames = {item["name"] for item in dir_listing}

    system_files_present = {"RGT.SYS", "GRT.SYS", "DIRECT.SYS", "HDOS.SYS"} & filenames

    for sys_file in system_files_present:
        with pytest.raises(IOError, match="Cannot delete system file"):
            hdos_controller.filesystem.delete(f"/{sys_file}")

    dir_listing_after = hdos_controller.list_directory("/")
    assert len(dir_listing_after) == len(dir_listing)

    hdos_controller.close_disk()


def test_format_creates_structures(
    hdos_controller: DiskController, tmp_path: Path
) -> None:
    """Tests that formatting creates all necessary HDOS structures at correct locations."""
    profile_name = "hdos_5.25_100k"
    profile = hdos_controller.get_format_by_name(profile_name)
    assert profile

    blank_img_path = tmp_path / "format_test.h8d"
    blank_img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)

    assert hdos_controller.open_disk(
        str(blank_img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert hdos_controller.format_disk_media(profile_name)

    fs = hdos_controller.filesystem
    assert isinstance(fs, HDOSFilesystem)

    if fs.label is None:
        hdos_controller.close_disk()
        assert hdos_controller.open_disk(
            str(blank_img_path),
            disk_type="IMG",
            format_info={"format_name": profile_name},
        )
        fs = hdos_controller.filesystem
        assert isinstance(fs, HDOSFilesystem)

    assert fs.label is not None
    assert fs.label.volume_number == 1
    assert fs.label.cluster_factor == 2

    assert fs.label.grt_start_block > 0
    grt_data = fs._read_lba(fs.label.grt_start_block)
    assert len(grt_data) == 256
    assert grt_data[0] != 0

    rgt_data = fs._read_lba(HDOS_RGT_SECTOR_LBA)
    assert len(rgt_data) == 256

    assert fs.label.dir_start_block > 0
    dir_entries = hdos_controller.list_directory("/")
    assert dir_entries == []

    hdos_controller.close_disk()


def test_rgt_lockout_functionality(
    hdos_controller: DiskController, tmp_path: Path
) -> None:
    """Tests that groups locked out in the RGT are correctly identified and excluded."""
    profile_name = "hdos_5.25_100k"
    profile = hdos_controller.get_format_by_name(profile_name)
    assert profile

    blank_img_path = tmp_path / "rgt_test.h8d"
    blank_img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)

    assert hdos_controller.open_disk(
        str(blank_img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert hdos_controller.format_disk_media(profile_name)

    fs = hdos_controller.filesystem

    if fs._rgt:
        for group in range(1, 5):
            assert fs._is_group_locked_out(group)

    allocated = fs.get_allocated_units()

    for group in range(1, 5):
        assert group in allocated

    hdos_controller.close_disk()


def test_write_to_full_disk(hdos_controller: DiskController, tmp_path: Path) -> None:
    """Tests behavior when attempting to write to a full or nearly-full disk."""
    image_file = "HDOS_2-0_TEST.h8d"
    temp_img_path = tmp_path / image_file
    shutil.copy(HDOS_RESOURCE_DIR / image_file, temp_img_path)

    assert hdos_controller.open_disk(str(temp_img_path), disk_type="IMG")

    free_bytes, _ = hdos_controller.get_free_space()

    large_data = b"X" * (free_bytes + 1024)

    with pytest.raises(IOError, match="Not enough free space"):
        hdos_controller.write_file("/TOOLARGE.TXT", large_data)

    dir_after = hdos_controller.list_directory("/")
    assert dir_after is not None

    hdos_controller.close_disk()


def test_empty_file_operations(hdos_controller: DiskController, tmp_path: Path) -> None:
    """Tests creating, reading, and deleting empty (0-byte) files."""
    profile_name = "hdos_5.25_100k"
    profile = hdos_controller.get_format_by_name(profile_name)
    blank_img_path = tmp_path / "empty_test.h8d"
    blank_img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)

    assert hdos_controller.open_disk(
        str(blank_img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert hdos_controller.format_disk_media(profile_name)

    assert hdos_controller.write_file("/EMPTY.TXT", b"")

    dir_listing = hdos_controller.list_directory("/")
    assert any(f["name"] == "EMPTY.TXT" for f in dir_listing)

    content = hdos_controller.read_file("/EMPTY.TXT")
    assert content == b""

    empty_file_entry = next(f for f in dir_listing if f["name"] == "EMPTY.TXT")
    assert empty_file_entry["size"] == 0

    assert hdos_controller.delete_item_recursive("/EMPTY.TXT")

    dir_after = hdos_controller.list_directory("/")
    assert not any(f["name"] == "EMPTY.TXT" for f in dir_after)

    hdos_controller.close_disk()


def test_multiple_file_operations(
    hdos_controller: DiskController, tmp_path: Path
) -> None:
    """Tests multiple sequential file operations to verify free space management."""
    profile_name = "hdos_5.25_100k"
    profile = hdos_controller.get_format_by_name(profile_name)
    blank_img_path = tmp_path / "multi_test.h8d"
    blank_img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)

    assert hdos_controller.open_disk(
        str(blank_img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert hdos_controller.format_disk_media(profile_name)

    initial_free, _ = hdos_controller.get_free_space()

    files = {
        "FILE1.TXT": b"Content of file one\n" * 50,
        "FILE2.TXT": b"Second file content\n" * 100,
        "FILE3.TXT": b"Third file here\n" * 75,
    }

    for filename, content in files.items():
        assert hdos_controller.write_file(f"/{filename}", content)

    dir_listing = hdos_controller.list_directory("/")
    assert len(dir_listing) == 3

    free_after_write, _ = hdos_controller.get_free_space()
    assert free_after_write < initial_free

    assert hdos_controller.delete_item_recursive("/FILE2.TXT")

    free_after_delete, _ = hdos_controller.get_free_space()
    assert free_after_delete > free_after_write

    assert free_after_delete < initial_free

    assert hdos_controller.write_file("/FILE4.TXT", b"New file\n" * 50)

    final_listing = hdos_controller.list_directory("/")
    final_names = {f["name"] for f in final_listing}
    assert final_names == {"FILE1.TXT", "FILE3.TXT", "FILE4.TXT"}

    for filename in final_names:
        content = hdos_controller.read_file(f"/{filename}")
        assert len(content) > 0

    hdos_controller.close_disk()


def test_free_chain_rebuild(hdos_controller: DiskController, tmp_path: Path) -> None:
    """Tests that the free chain is properly rebuilt when GRT[0] = 0."""
    image_file = "HDOS_2-0_TEST.h8d"
    temp_img_path = tmp_path / image_file
    shutil.copy(HDOS_RESOURCE_DIR / image_file, temp_img_path)

    assert hdos_controller.open_disk(str(temp_img_path), disk_type="IMG")

    fs = hdos_controller.filesystem
    initial_grt_0 = fs._grt[0]

    assert initial_grt_0 == 0

    assert hdos_controller.delete_item_recursive("/BITS.ACM")

    fs._init_completed = False
    fs._initialize()

    assert fs._grt[0] != 0

    free_bytes, _ = hdos_controller.get_free_space()
    assert free_bytes > 0

    assert hdos_controller.write_file("/NEWFILE.TXT", b"Test content\n" * 20)

    content = hdos_controller.read_file("/NEWFILE.TXT")
    assert b"Test content" in content

    hdos_controller.close_disk()


def test_text_file_null_trimming(
    hdos_controller: DiskController, tmp_path: Path
) -> None:
    """Tests that text files have trailing nulls trimmed upon reading."""
    profile_name = "hdos_5.25_100k"
    profile = hdos_controller.get_format_by_name(profile_name)
    blank_img_path = tmp_path / "trim_test.h8d"
    blank_img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)

    assert hdos_controller.open_disk(
        str(blank_img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert hdos_controller.format_disk_media(profile_name)

    text_content = b"This is a text file"
    assert hdos_controller.write_file("/TEXT.TXT", text_content)

    read_text = hdos_controller.read_file("/TEXT.TXT")
    assert read_text == text_content
    assert read_text[-1] != 0

    binary_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00"
    assert hdos_controller.write_file("/BINARY.DAT", binary_content)

    read_binary = hdos_controller.read_file("/BINARY.DAT")
    assert read_binary.startswith(binary_content)

    hdos_controller.close_disk()


def test_large_file_spanning_many_groups(
    hdos_controller: DiskController, tmp_path: Path
) -> None:
    """Tests writing and reading a large file that spans 50+ allocation groups."""
    profile_name = "hdos_5.25_100k"
    profile = hdos_controller.get_format_by_name(profile_name)
    blank_img_path = tmp_path / "large_file_test.h8d"
    blank_img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)

    assert hdos_controller.open_disk(
        str(blank_img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert hdos_controller.format_disk_media(profile_name)

    groups_needed = 60
    bytes_per_group = 512
    file_size = groups_needed * bytes_per_group

    pattern = b"HDOS_LARGE_FILE_TEST_PATTERN_" + bytes(range(256))
    large_content = (pattern * (file_size // len(pattern) + 1))[:file_size]

    initial_free, _ = hdos_controller.get_free_space()
    assert initial_free >= file_size

    filename = "/LARGEFIL.DAT"
    assert hdos_controller.write_file(filename, large_content)

    free_after_write, _ = hdos_controller.get_free_space()
    assert free_after_write < initial_free

    dir_listing = hdos_controller.list_directory("/")
    large_file_entry = next(
        (f for f in dir_listing if f["name"] == "LARGEFIL.DAT"), None
    )
    assert large_file_entry is not None
    assert large_file_entry["size"] == file_size

    read_content = hdos_controller.read_file(filename)
    assert len(read_content) == len(large_content)
    assert read_content == large_content

    assert hdos_controller.delete_item_recursive(filename)
    free_after_delete, _ = hdos_controller.get_free_space()
    assert free_after_delete > free_after_write

    second_file_content = b"Testing reuse of freed space" * 100
    assert hdos_controller.write_file("/SECOND.TXT", second_file_content)
    read_second = hdos_controller.read_file("/SECOND.TXT")
    assert read_second == second_file_content

    hdos_controller.close_disk()


def test_filename_edge_cases(hdos_controller: DiskController, tmp_path: Path) -> None:
    """Tests filename edge cases including 8.3 limits, case sensitivity, and truncation."""
    profile_name = "hdos_5.25_100k"
    profile = hdos_controller.get_format_by_name(profile_name)
    blank_img_path = tmp_path / "filename_test.h8d"
    blank_img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)

    assert hdos_controller.open_disk(
        str(blank_img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert hdos_controller.format_disk_media(profile_name)

    test_content = b"Test content for filename tests"

    assert hdos_controller.write_file("/MAXNAME8.EXT", test_content)
    assert any(f["name"] == "MAXNAME8.EXT" for f in hdos_controller.list_directory("/"))

    assert hdos_controller.write_file("/A.B", test_content)
    assert any(f["name"] == "A.B" for f in hdos_controller.list_directory("/"))

    assert hdos_controller.write_file("/NOEXT", test_content)
    assert any(f["name"] == "NOEXT" for f in hdos_controller.list_directory("/"))

    assert hdos_controller.write_file("/lowercasefile.longext", test_content)
    assert any(f["name"] == "LOWERCAS.LON" for f in hdos_controller.list_directory("/"))

    read_content = hdos_controller.read_file("/LOWERCAS.LON")
    assert read_content == test_content

    original_count = len(hdos_controller.list_directory("/"))
    modified_content = b"Modified content"
    assert hdos_controller.write_file("/A.B", modified_content)
    assert len(hdos_controller.list_directory("/")) == original_count
    assert hdos_controller.read_file("/A.B") == modified_content

    hdos_controller.close_disk()


def test_read_only_operations_no_modification(
    hdos_controller: DiskController, tmp_path: Path
) -> None:
    """Tests that read-only operations do not modify the disk image."""
    image_file = "HDOS_2-0_TEST.h8d"
    temp_img_path = tmp_path / image_file
    shutil.copy(HDOS_RESOURCE_DIR / image_file, temp_img_path)

    initial_hash = hashlib.sha256(temp_img_path.read_bytes()).hexdigest()

    assert hdos_controller.open_disk(str(temp_img_path), disk_type="IMG")
    assert isinstance(hdos_controller.filesystem, HDOSFilesystem)

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

    hdos_controller.close_disk()

    final_hash = hashlib.sha256(temp_img_path.read_bytes()).hexdigest()

    assert initial_hash == final_hash

    assert hdos_controller.open_disk(str(temp_img_path), disk_type="IMG")
    final_dir_listing = hdos_controller.list_directory("/")
    assert final_dir_listing == dir_listing_1

    final_content = hdos_controller.read_file("/DVDIO.ACM")
    assert final_content == content_1

    hdos_controller.close_disk()
