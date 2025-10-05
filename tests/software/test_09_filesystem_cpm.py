"""
Pytest module for testing the CPMFilesystem through the DiskController.

This module focuses on high-level integration tests that simulate user
actions on CP/M formatted disk images.
"""

import math
import shutil
import sys
from pathlib import Path
from typing import Iterator, List

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers import IMGImageDriver
from fatfloppy.core.filesystems.cpm_fs import CPM_SECTOR_SIZE, CPMFilesystem

RESOURCE_DIR = Path(__file__).parent.parent / "resources"
CPM_RESOURCE_DIR = RESOURCE_DIR / "CPM"
FAT_IMG_SRC = RESOURCE_DIR / "empty_formatted_144m.img"


@pytest.fixture(scope="function")
def cpm_controller() -> Iterator[DiskController]:
    """
    Provides a clean DiskController instance for each test and handles cleanup.

    Yields:
        A new DiskController instance for testing.
    """
    controller = DiskController()
    yield controller
    if controller.disk:
        controller.close_disk()


@pytest.mark.parametrize(
    "image_file, disk_type, expected_format_name, expected_free_space_kb, expected_files",
    [
        (
            "disk1.img",
            "IMG",
            "cpm_8_sssd_250k_interleave6",
            35,
            ["2FBIOS24.ASM", "DISKTEST.ASM", "READ.ME", "CPM.COM"],
        ),
        (
            "disk17.imd",
            "IMD",
            "cpm_8_ssdd_imsai_mixed",
            242,
            ["EBASIC.COM", "DENSITY.ASM", "SOLUSER.ASM", "CPM56.COM"],
        ),
    ],
)
def test_disk_images_read_and_verify(
    cpm_controller: DiskController,
    tmp_path: Path,
    image_file: str,
    disk_type: str,
    expected_format_name: str,
    expected_free_space_kb: int,
    expected_files: List[str],
) -> None:
    """
    Tests opening CP/M disk images (IMG and IMD), verifying format detection,
    directory listing, free space, and file content integrity.
    """
    img_path = CPM_RESOURCE_DIR / image_file
    success = cpm_controller.open_disk(str(img_path), disk_type=disk_type)
    assert success
    assert cpm_controller.disk is not None
    assert isinstance(cpm_controller.filesystem, CPMFilesystem)

    detected_format_name, _, _ = cpm_controller.detect_format()
    assert detected_format_name == expected_format_name

    profile = cpm_controller.get_format_by_name(expected_format_name)
    assert profile is not None
    assert profile.physical_format.track_formats[0].encoding == "FM"

    dir_listing = cpm_controller.list_directory("/")
    assert dir_listing

    listed_filenames = {item["name"] for item in dir_listing}
    for fname in expected_files:
        assert fname in listed_filenames
        assert all(32 <= ord(c) < 127 for c in fname if c not in ".")

    free_bytes, _ = cpm_controller.get_free_space()
    free_kb = free_bytes / 1024
    assert free_kb == pytest.approx(expected_free_space_kb, abs=1)

    for filename in expected_files:
        cpm_filepath = f"/{filename}"
        content_from_disk = cpm_controller.read_file(cpm_filepath)
        assert content_from_disk is not None

        ground_truth_path = CPM_RESOURCE_DIR / filename
        content_from_file = ground_truth_path.read_bytes()

        content_from_disk_norm = (
            content_from_disk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        )
        content_from_file_norm = (
            content_from_file.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        )

        assert content_from_disk_norm == content_from_file_norm

        (tmp_path / filename).write_bytes(content_from_disk)

    cpm_controller.close_disk()


@pytest.mark.parametrize(
    "image_file, disk_type, file_to_test",
    [
        ("disk1.img", "IMG", "DISKTEST.ASM"),
        ("disk17.imd", "IMD", "DENSITY.ASM"),
    ],
)
def test_file_write_delete_and_verify(
    cpm_controller: DiskController,
    tmp_path: Path,
    image_file: str,
    disk_type: str,
    file_to_test: str,
) -> None:
    """Tests delete -> write -> read cycle for a file on a CP/M image."""
    original_img_path = CPM_RESOURCE_DIR / image_file
    temp_img_path = tmp_path / image_file
    shutil.copy(original_img_path, temp_img_path)

    ground_truth_path = CPM_RESOURCE_DIR / file_to_test
    ground_truth_content = ground_truth_path.read_bytes()
    assert ground_truth_content

    format_name = "cpm_8_sssd_250k" if image_file == "disk1.img" else None
    format_info = {"format_name": format_name} if format_name else None

    success = cpm_controller.open_disk(
        str(temp_img_path), disk_type=disk_type, format_info=format_info
    )
    assert success
    assert isinstance(cpm_controller.filesystem, CPMFilesystem)

    initial_dir = cpm_controller.list_directory("/")
    initial_filenames = {item["name"] for item in initial_dir}
    assert file_to_test in initial_filenames

    initial_content = cpm_controller.read_file(f"/{file_to_test}")
    assert initial_content is not None

    file_blocks = cpm_controller.get_file_allocation_units(f"/{file_to_test}")
    assert file_blocks is not None and len(file_blocks) > 0

    block_size = cpm_controller.filesystem.allocation_unit_size
    actual_allocated_space = len(file_blocks) * block_size

    initial_free_bytes, _ = cpm_controller.get_free_space()

    delete_success = cpm_controller.delete_item(f"/{file_to_test}")
    assert delete_success

    dir_after_delete = cpm_controller.list_directory("/")
    filenames_after_delete = {item["name"] for item in dir_after_delete}
    assert file_to_test not in filenames_after_delete

    free_bytes_after_delete, _ = cpm_controller.get_free_space()

    assert free_bytes_after_delete == pytest.approx(
        initial_free_bytes + actual_allocated_space
    )

    write_success = cpm_controller.write_file(f"/{file_to_test}", ground_truth_content)
    assert write_success

    dir_after_write = cpm_controller.list_directory("/")
    filenames_after_write = {item["name"] for item in dir_after_write}
    assert file_to_test in filenames_after_write

    read_back_content = cpm_controller.read_file(f"/{file_to_test}")
    assert read_back_content is not None
    assert read_back_content == ground_truth_content

    final_free_bytes, _ = cpm_controller.get_free_space()

    max_diff = 2 * block_size
    assert abs(final_free_bytes - initial_free_bytes) <= max_diff

    cpm_controller.close_disk()


def test_format_and_write(cpm_controller: DiskController, tmp_path: Path) -> None:
    """Tests the format_disk functionality for CP/M."""
    profile_name = "cpm_8_sssd_250k"
    profile = cpm_controller.get_format_by_name(profile_name)
    assert profile

    blank_img_path = tmp_path / "blank.img"
    total_bytes = profile.physical_format.total_bytes
    blank_img_path.write_bytes(b"\x00" * total_bytes)

    assert cpm_controller.open_disk(
        str(blank_img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    format_success = cpm_controller.format_disk_media(profile_name)
    assert format_success

    assert isinstance(cpm_controller.filesystem, CPMFilesystem)
    assert cpm_controller.list_directory("/") == []

    dpb = cpm_controller.filesystem.dpb
    dir_blocks = dpb.directory_blocks
    block_size = dpb.block_size
    total_data_bytes = (dpb.dsm + 1) * block_size
    expected_free_bytes = total_data_bytes - (dir_blocks * block_size)

    free_bytes, _ = cpm_controller.get_free_space()
    assert free_bytes == expected_free_bytes

    test_content = b"This is a test file after formatting."
    write_success = cpm_controller.write_file("/TEST.TXT", test_content)
    assert write_success

    read_content = cpm_controller.read_file("/TEST.TXT")
    assert read_content == test_content

    cpm_controller.close_disk()


def test_directory_full_error(cpm_controller: DiskController, tmp_path: Path) -> None:
    """Tests that writing a file fails with an IOError when the directory is full."""
    profile_name = "cpm_8_sssd_250k"
    profile = cpm_controller.get_format_by_name(profile_name)
    assert profile

    img_path = tmp_path / "dir_full.img"
    img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert cpm_controller.open_disk(
        str(img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert cpm_controller.format_disk_media(profile_name)

    dpb = cpm_controller.filesystem.dpb
    max_entries = dpb.drm + 1
    for i in range(max_entries):
        filename = f"/FILE{i}.TXT"
        assert cpm_controller.write_file(filename, b"small")

    assert len(cpm_controller.list_directory("/")) == max_entries

    with pytest.raises(IOError, match="Directory is full"):
        cpm_controller.write_file("/EXTRA.TXT", b"this should fail")

    cpm_controller.close_disk()


def test_disk_full_error(cpm_controller: DiskController, tmp_path: Path) -> None:
    """Tests that writing a file fails with an IOError when the data area is full."""
    profile_name = "cpm_8_sssd_250k"
    profile = cpm_controller.get_format_by_name(profile_name)
    assert profile

    img_path = tmp_path / "disk_full.img"
    img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert cpm_controller.open_disk(
        str(img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert cpm_controller.format_disk_media(profile_name)

    free_bytes, _ = cpm_controller.get_free_space()
    data_to_fill_disk = b"\xAA" * (
        free_bytes - (profile.filesystem_config.block_size - 1)
    )

    assert cpm_controller.write_file("/BIGFILE.BIN", data_to_fill_disk)

    with pytest.raises(IOError, match="Not enough free space"):
        cpm_controller.write_file("/SMALL.BIN", b"no space for this")

    cpm_controller.close_disk()


def test_validity_score(cpm_controller: DiskController) -> None:
    """Tests the get_validity_score method against different disk types."""
    cpm_img_path = CPM_RESOURCE_DIR / "disk1.img"
    if not cpm_img_path.exists():
        pytest.skip(f"CP/M test resource not found: {cpm_img_path}")

    cpm_controller.open_disk(str(cpm_img_path), disk_type="IMG")
    assert isinstance(cpm_controller.filesystem, CPMFilesystem)
    assert (
        cpm_controller.filesystem.get_validity_score()
        >= CPMFilesystem.VALIDITY_THRESHOLD
    )
    cpm_controller.close_disk()

    if not FAT_IMG_SRC.exists():
        pytest.skip(f"FAT test resource not found: {FAT_IMG_SRC}")

    fat_driver = IMGImageDriver(str(FAT_IMG_SRC))
    fat_disk = Disk(fat_driver)
    cpm_profile = cpm_controller.get_format_by_name("cpm_8_sssd_250k")
    assert cpm_profile is not None
    fat_disk.set_geometry(cpm_profile.physical_format)

    cpm_fs_on_fat_disk = CPMFilesystem(fat_disk, config=cpm_profile.filesystem_config)
    assert (
        cpm_fs_on_fat_disk.get_validity_score() < CPMFilesystem.VALIDITY_THRESHOLD
    )

    garbage_data = b"random garbage data" * 20000
    garbage_driver = IMGImageDriver("garbage.img", image_data=garbage_data)
    garbage_disk = Disk(garbage_driver)
    garbage_disk.set_geometry(cpm_profile.physical_format)

    cpm_fs_on_garbage = CPMFilesystem(garbage_disk, config=cpm_profile.filesystem_config)
    assert cpm_fs_on_garbage.get_validity_score() == 0


def test_single_byte_file_write_read(
    cpm_controller: DiskController, tmp_path: Path
) -> None:
    """Tests writing and reading a very small (1-byte) file."""
    profile_name = "cpm_8_sssd_250k"
    profile = cpm_controller.get_format_by_name(profile_name)
    assert profile

    img_path = tmp_path / "small_file_test.img"
    img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert cpm_controller.open_disk(
        str(img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert cpm_controller.format_disk_media(profile_name)

    minimal_data = b"X"
    assert cpm_controller.write_file("/TINY.TXT", minimal_data)

    dir_listing = cpm_controller.list_directory("/")
    assert len(dir_listing) == 1
    assert dir_listing[0]["name"] == "TINY.TXT"
    assert dir_listing[0]["size"] == CPM_SECTOR_SIZE

    content = cpm_controller.read_file("/TINY.TXT")
    assert content.startswith(minimal_data)

    cpm_controller.close_disk()


def test_file_exact_extent_boundary(
    cpm_controller: DiskController, tmp_path: Path
) -> None:
    """Tests writing a file that exactly fills one extent (16KB)."""
    profile_name = "cpm_8_sssd_250k"
    profile = cpm_controller.get_format_by_name(profile_name)
    assert profile

    img_path = tmp_path / "extent_boundary.img"
    img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert cpm_controller.open_disk(
        str(img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert cpm_controller.format_disk_media(profile_name)

    exact_extent_data = b"X" * 16384
    assert cpm_controller.write_file("/EXACT.BIN", exact_extent_data)

    read_data = cpm_controller.read_file("/EXACT.BIN")
    assert len(read_data) == 16384
    assert read_data == exact_extent_data

    cpm_controller.close_disk()


def test_file_multiple_extents(cpm_controller: DiskController, tmp_path: Path) -> None:
    """Tests writing and reading a file that spans multiple extents (>16KB)."""
    profile_name = "cpm_8_sssd_250k"
    profile = cpm_controller.get_format_by_name(profile_name)
    assert profile

    img_path = tmp_path / "multi_extent.img"
    img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert cpm_controller.open_disk(
        str(img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert cpm_controller.format_disk_media(profile_name)

    multi_extent_data = bytes(range(256)) * 160
    assert cpm_controller.write_file("/LARGE.DAT", multi_extent_data)

    dir_listing = cpm_controller.list_directory("/")
    assert len(dir_listing) == 1
    assert dir_listing[0]["size"] == len(multi_extent_data)

    read_data = cpm_controller.read_file("/LARGE.DAT")
    assert read_data == multi_extent_data

    cpm_controller.close_disk()


def test_user_areas_multiple_files(
    cpm_controller: DiskController, tmp_path: Path
) -> None:
    """Tests writing and reading files in different user areas (U0-U15)."""
    profile_name = "cpm_8_sssd_250k"
    profile = cpm_controller.get_format_by_name(profile_name)
    assert profile

    img_path = tmp_path / "user_areas.img"
    img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert cpm_controller.open_disk(
        str(img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert cpm_controller.format_disk_media(profile_name)

    test_data = {
        "U0:FILE.TXT": b"User 0 content",
        "U1:FILE.TXT": b"User 1 content",
        "U5:DATA.BIN": b"User 5 data",
        "U15:LAST.DOC": b"User 15 document",
    }

    for path, data in test_data.items():
        assert cpm_controller.write_file(f"/{path}", data)

    for path, expected_data in test_data.items():
        read_data = cpm_controller.read_file(f"/{path}")
        assert read_data == expected_data

    dir_listing = cpm_controller.list_directory("/")
    assert len(dir_listing) == 4

    for item in dir_listing:
        assert item["attributes"].startswith("U")

    cpm_controller.close_disk()


def test_same_filename_different_users(
    cpm_controller: DiskController, tmp_path: Path
) -> None:
    """Tests handling files with the same name in different user areas."""
    profile_name = "cpm_8_sssd_250k"
    profile = cpm_controller.get_format_by_name(profile_name)
    assert profile

    img_path = tmp_path / "same_name.img"
    img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert cpm_controller.open_disk(
        str(img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert cpm_controller.format_disk_media(profile_name)

    assert cpm_controller.write_file("/U0:TEST.TXT", b"Content from user 0")
    assert cpm_controller.write_file("/U1:TEST.TXT", b"Content from user 1")
    assert cpm_controller.write_file("/U2:TEST.TXT", b"Content from user 2")

    assert cpm_controller.read_file("/U0:TEST.TXT") == b"Content from user 0"
    assert cpm_controller.read_file("/U1:TEST.TXT") == b"Content from user 1"
    assert cpm_controller.read_file("/U2:TEST.TXT") == b"Content from user 2"

    content = cpm_controller.read_file("/TEST.TXT")
    assert content == b"Content from user 0"

    cpm_controller.close_disk()


def test_invalid_filename_handling(
    cpm_controller: DiskController, tmp_path: Path
) -> None:
    """Tests error handling for invalid filenames (e.g., too long)."""
    profile_name = "cpm_8_sssd_250k"
    profile = cpm_controller.get_format_by_name(profile_name)
    assert profile

    img_path = tmp_path / "invalid_names.img"
    img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert cpm_controller.open_disk(
        str(img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert cpm_controller.format_disk_media(profile_name)

    result = cpm_controller.read_file("/NOEXTENSION")
    assert result is None

    long_name_data = b"test content"
    assert cpm_controller.write_file("/VERYLONGFILENAME.LONGEXT", long_name_data)

    dir_listing = cpm_controller.list_directory("/")
    assert len(dir_listing) == 1
    name, ext = dir_listing[0]["name"].split(".")
    assert len(name) <= 8
    assert len(ext) <= 3

    cpm_controller.close_disk()


def test_delete_specific_user_file(
    cpm_controller: DiskController, tmp_path: Path
) -> None:
    """Tests deleting a file from a specific user area without affecting others."""
    profile_name = "cpm_8_sssd_250k"
    profile = cpm_controller.get_format_by_name(profile_name)
    assert profile

    img_path = tmp_path / "delete_user.img"
    img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert cpm_controller.open_disk(
        str(img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert cpm_controller.format_disk_media(profile_name)

    assert cpm_controller.write_file("/U0:DATA.BIN", b"User 0 data")
    assert cpm_controller.write_file("/U1:DATA.BIN", b"User 1 data")
    assert cpm_controller.write_file("/U2:DATA.BIN", b"User 2 data")

    assert cpm_controller.delete_item("/U1:DATA.BIN")

    u1_result = cpm_controller.read_file("/U1:DATA.BIN")
    assert u1_result is None

    assert cpm_controller.read_file("/U0:DATA.BIN") == b"User 0 data"
    assert cpm_controller.read_file("/U2:DATA.BIN") == b"User 2 data"

    dir_listing = cpm_controller.list_directory("/")
    assert len(dir_listing) == 2

    cpm_controller.close_disk()


def test_overwrite_existing_file(cpm_controller: DiskController, tmp_path: Path) -> None:
    """Tests overwriting an existing file with new content of different size."""
    profile_name = "cpm_8_sssd_250k"
    profile = cpm_controller.get_format_by_name(profile_name)
    assert profile

    img_path = tmp_path / "overwrite.img"
    img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert cpm_controller.open_disk(
        str(img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert cpm_controller.format_disk_media(profile_name)

    initial_data = b"Initial content that is moderately long"
    assert cpm_controller.write_file("/TEST.DAT", initial_data)
    assert cpm_controller.read_file("/TEST.DAT") == initial_data

    larger_data = b"X" * 5000
    assert cpm_controller.write_file("/TEST.DAT", larger_data)
    assert cpm_controller.read_file("/TEST.DAT") == larger_data

    smaller_data = b"Small"
    assert cpm_controller.write_file("/TEST.DAT", smaller_data)
    read_back = cpm_controller.read_file("/TEST.DAT")
    assert read_back == smaller_data

    dir_listing = cpm_controller.list_directory("/")
    assert len(dir_listing) == 1

    cpm_controller.close_disk()


def test_free_space_tracking(cpm_controller: DiskController, tmp_path: Path) -> None:
    """Tests that free space is accurately tracked through various operations."""
    profile_name = "cpm_8_sssd_250k"
    profile = cpm_controller.get_format_by_name(profile_name)
    assert profile

    img_path = tmp_path / "freespace.img"
    img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert cpm_controller.open_disk(
        str(img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert cpm_controller.format_disk_media(profile_name)

    initial_free, total = cpm_controller.get_free_space()
    assert initial_free > 0
    assert total > initial_free

    test_data = b"A" * 3000
    assert cpm_controller.write_file("/FILE1.TXT", test_data)
    free_after_write, _ = cpm_controller.get_free_space()
    assert free_after_write < initial_free

    assert cpm_controller.write_file("/FILE2.TXT", test_data)
    free_after_second, _ = cpm_controller.get_free_space()
    assert free_after_second < free_after_write

    assert cpm_controller.delete_item("/FILE1.TXT")
    free_after_delete, _ = cpm_controller.get_free_space()
    assert free_after_delete > free_after_second

    assert cpm_controller.delete_item("/FILE2.TXT")
    final_free, _ = cpm_controller.get_free_space()
    assert final_free == pytest.approx(initial_free)

    cpm_controller.close_disk()


def test_file_with_no_extension(cpm_controller: DiskController, tmp_path: Path) -> None:
    """Tests writing and reading a file specified without an extension."""
    profile_name = "cpm_8_sssd_250k"
    profile = cpm_controller.get_format_by_name(profile_name)
    assert profile

    img_path = tmp_path / "no_ext.img"
    img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert cpm_controller.open_disk(
        str(img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert cpm_controller.format_disk_media(profile_name)

    test_data = b"Content without extension"
    filename = "/NOEXT."
    assert cpm_controller.write_file(filename, test_data)

    read_data = cpm_controller.read_file(filename)
    assert read_data == test_data

    dir_listing = cpm_controller.list_directory("/")
    assert len(dir_listing) == 1
    assert dir_listing[0]["name"] == "NOEXT."

    cpm_controller.close_disk()


def test_allocated_blocks_consistency(
    cpm_controller: DiskController, tmp_path: Path
) -> None:
    """Tests that the get_allocated_units method accurately tracks block allocation."""
    profile_name = "cpm_8_sssd_250k"
    profile = cpm_controller.get_format_by_name(profile_name)
    assert profile

    img_path = tmp_path / "alloc_blocks.img"
    img_path.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert cpm_controller.open_disk(
        str(img_path), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert cpm_controller.format_disk_media(profile_name)

    initial_allocated = cpm_controller.filesystem.get_allocated_units()
    dpb = cpm_controller.filesystem.dpb
    assert len(initial_allocated) == dpb.directory_blocks

    test_data = b"X" * 2500
    assert cpm_controller.write_file("/TEST.BIN", test_data)

    after_write_allocated = cpm_controller.filesystem.get_allocated_units()
    assert len(after_write_allocated) > len(initial_allocated)

    assert cpm_controller.delete_item("/TEST.BIN")

    final_allocated = cpm_controller.filesystem.get_allocated_units()
    assert len(final_allocated) == len(initial_allocated)

    cpm_controller.close_disk()
