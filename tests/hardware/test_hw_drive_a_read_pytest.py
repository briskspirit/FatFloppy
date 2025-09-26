# tests/hardware/test_hw_drive_a_read_pytest.py
"""
Hardware tests for read operations on Drive A using a 1.44M formatted disk.

This test suite verifies the functionality of the DiskController for reading
directory listings, file contents, and disk information from a physical
3.5" floppy drive.

It requires the 'TEST_DRIVE_A' environment variable to be set to 'true'.
The tests will:
1.  Use Greaseweazle to write a pre-populated 1.44M disk image.
2.  Initialize the DiskController with the physical drive.
3.  Perform various read operations (list directories, read files, get info).
4.  Assert that the outcomes match the expected contents of the image.
"""

import os
from typing import Dict, Generator

import pytest
from _pytest.config import Config

from fatfloppy.core.controller import DiskController
from .conftest import (GW_FORMAT_144M, POPULATED_144M_IMG, _run_gw_write)

# --- Test Configuration ---

TARGET_DRIVE: str = 'A'
TARGET_FORMAT_KEY: str = '1.44M'
TARGET_DRIVE_SIZE: str = "3.5"

# Apply markers to all tests in this module.
pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif(
        os.getenv('TEST_DRIVE_A', 'false').lower() != 'true',
        reason=(
            "Requires TEST_DRIVE_A='true' environment variable to be set. "
            f"These tests target Drive {TARGET_DRIVE} with a {TARGET_FORMAT_KEY} disk."
        )
    )
]


# --- Fixtures ---

@pytest.fixture(scope="class")
def prepared_controller(
    pytestconfig: Config,
    expected_file_content: Dict[str, bytes]
) -> Generator[DiskController, None, None]:
    """
    Class-scoped fixture to prepare a physical floppy disk for read tests.

    This fixture performs the following setup actions:
    1.  Writes a populated 1.44M disk image to the floppy in Drive A
        using the Greaseweazle tool.
    2.  Initializes a DiskController and opens the physical drive.
    3.  Verifies that a valid FAT filesystem is detected.
    4.  Attaches the expected file content to the controller for easy access
        in tests.

    After all tests in the class have run, it performs teardown by closing
    the connection to the disk.

    Args:
        pytestconfig: The pytest configuration object, used to get command-line options.
        expected_file_content: Fixture providing the expected content of test files.

    Yields:
        An initialized and ready-to-use DiskController instance.
    """
    print(f"\n--- Setting up Hardware Test Class for Drive {TARGET_DRIVE} ({TARGET_FORMAT_KEY} Read) ---")
    auto_mode: bool = pytestconfig.getoption("--hw-auto")

    # Write the populated disk image to the physical floppy.
    write_success = _run_gw_write(
        TARGET_DRIVE,
        POPULATED_144M_IMG,
        GW_FORMAT_144M,
        f"Populated {TARGET_FORMAT_KEY} for Read Tests",
        auto_mode
    )
    if not write_success:
        pytest.skip(f"Skipping tests for Drive {TARGET_DRIVE} due to write failure.", allow_module_level=True)

    print(f"Attempting to open Drive {TARGET_DRIVE}...")
    controller = DiskController()
    gw_device: str | None = os.environ.get('GW_DEVICE', None)

    success = controller.open_disk(
        source=gw_device,
        disk_type="physical",
        drive_letter=TARGET_DRIVE,
        drive_size=TARGET_DRIVE_SIZE
    )

    if not success:
        controller.close_disk()
        pytest.skip(
            f"Failed to open physical drive {TARGET_DRIVE} after preparation. "
            "Check connection/disk.",
            allow_module_level=True
        )

    if not controller.filesystem or not controller.filesystem.is_valid():
        controller.close_disk()
        pytest.skip(
            f"No valid filesystem detected on drive {TARGET_DRIVE} after preparation.",
            allow_module_level=True
        )

    print(f"Drive {TARGET_DRIVE} opened successfully.")
    # Attach expected content to the controller object for convenience in tests.
    controller.expected_content = expected_file_content

    yield controller

    print(f"\n--- Tearing down Hardware Test Class for Drive {TARGET_DRIVE} (Read) ---")
    controller.close_disk()
    print(f"Drive {TARGET_DRIVE} connection closed.")


# --- Test Class ---

@pytest.mark.usefixtures("prepared_controller")
class TestHardwareDriveARead:
    """
    A collection of read-only tests for a 1.44M floppy in Drive A.

    These tests assume the drive has been prepared by the `prepared_controller`
    fixture with a standard, populated filesystem.
    """

    def test_01_hw_A_list_root(self, prepared_controller: DiskController) -> None:
        """Verify the contents of the root directory ("/") are correct."""
        print("\nRunning: Verifying root directory listing...")
        controller = prepared_controller
        entries = controller.list_directory("/")

        assert entries is not None, "Listing root directory returned None."
        assert len(entries) >= 2, "Root directory has fewer than expected items (TEST.TXT, DIR1)"

        root_names = {e['name'].upper() for e in entries}
        assert 'TEST.TXT' in root_names, "TEST.TXT not found in root directory."
        assert 'DIR1' in root_names, "DIR1 not found in root directory."

        assert not next(e for e in entries if e['name'].upper() == 'TEST.TXT')['is_dir']
        assert next(e for e in entries if e['name'].upper() == 'DIR1')['is_dir']
        print(f"Found {len(entries)} items in root.")

    def test_02_hw_A_list_dir1(self, prepared_controller: DiskController) -> None:
        """Verify the contents of the subdirectory "/DIR1" are correct."""
        print("\nRunning: Verifying '/DIR1' directory listing...")
        controller = prepared_controller
        entries = controller.list_directory("/DIR1")

        assert entries is not None, "Listing '/DIR1' directory returned None."
        assert len(entries) >= 3, "/DIR1 has fewer than expected items (PATTERN.BIN, TEST.TXT, SUBDIR)"

        dir1_names = {e['name'].upper() for e in entries}
        assert 'PATTERN.BIN' in dir1_names
        assert 'TEST.TXT' in dir1_names
        assert 'SUBDIR' in dir1_names

        assert not next(e for e in entries if e['name'].upper() == 'PATTERN.BIN')['is_dir']
        assert not next(e for e in entries if e['name'].upper() == 'TEST.TXT')['is_dir']
        assert next(e for e in entries if e['name'].upper() == 'SUBDIR')['is_dir']
        print(f"Found {len(entries)} items in /DIR1.")

    def test_03_hw_A_list_subdir(self, prepared_controller: DiskController) -> None:
        """Verify the contents of the nested subdirectory "/DIR1/SUBDIR" are correct."""
        print("\nRunning: Verifying '/DIR1/SUBDIR' directory listing...")
        controller = prepared_controller
        entries = controller.list_directory("/DIR1/SUBDIR")

        assert entries is not None, "Listing '/DIR1/SUBDIR' directory returned None."
        assert len(entries) >= 1, "/DIR1/SUBDIR has fewer than expected items (TEST.TXT)"

        subdir_names = {e['name'].upper() for e in entries}
        assert 'TEST.TXT' in subdir_names

        assert not next(e for e in entries if e['name'].upper() == 'TEST.TXT')['is_dir']
        print(f"Found {len(entries)} items in /DIR1/SUBDIR.")

    def test_04_hw_A_read_root_file(self, prepared_controller: DiskController) -> None:
        """Verify reading a file from the root directory."""
        print("\nRunning: Reading file from root directory...")
        controller = prepared_controller
        filepath = "/TEST.TXT"
        content = controller.read_file(filepath)

        assert content is not None, f"Failed to read {filepath}"
        assert len(content) > 0, f"{filepath} is empty"

        if "test_txt" in controller.expected_content:
            assert content == controller.expected_content["test_txt"], f"Content mismatch for {filepath}"
        print(f"Read {len(content)} bytes from {filepath}")

    def test_05_hw_A_read_dir1_files(self, prepared_controller: DiskController) -> None:
        """Verify reading multiple files from the "/DIR1" subdirectory."""
        print("\nRunning: Reading files from '/DIR1' subdirectory...")
        controller = prepared_controller

        # Test 1: Read PATTERN.BIN
        filepath_bin = "/DIR1/PATTERN.BIN"
        content_bin = controller.read_file(filepath_bin)
        assert content_bin is not None, f"Failed to read {filepath_bin}"
        if "pattern_bin" in controller.expected_content:
            assert content_bin == controller.expected_content["pattern_bin"], f"Content mismatch for {filepath_bin}"
        print(f"Read {len(content_bin)} bytes from {filepath_bin}")

        # Test 2: Read TEST.TXT
        filepath_txt = "/DIR1/TEST.TXT"
        content_txt = controller.read_file(filepath_txt)
        assert content_txt is not None, f"Failed to read {filepath_txt}"
        if "test_txt" in controller.expected_content:
            assert content_txt == controller.expected_content["test_txt"], f"Content mismatch for {filepath_txt}"
        print(f"Read {len(content_txt)} bytes from {filepath_txt}")

    def test_06_hw_A_read_subdir_file(self, prepared_controller: DiskController) -> None:
        """Verify reading a file from the nested "/DIR1/SUBDIR" subdirectory."""
        print("\nRunning: Reading file from '/DIR1/SUBDIR'...")
        controller = prepared_controller
        filepath = "/DIR1/SUBDIR/TEST.TXT"
        content = controller.read_file(filepath)

        assert content is not None, f"Failed to read {filepath}"
        if "test_txt" in controller.expected_content:
            assert content == controller.expected_content["test_txt"], f"Content mismatch for {filepath}"
        print(f"Read {len(content)} bytes from {filepath}")

    def test_07_hw_A_get_disk_info(self, prepared_controller: DiskController) -> None:
        """Verify retrieval of disk space and allocation information."""
        print("\nRunning: Verifying disk information...")
        controller = prepared_controller

        free, total = controller.get_free_space()
        clusters = controller.get_allocated_units()

        assert free is not None and total is not None and clusters is not None
        assert free < total
        assert len(clusters) == 7, f"Expected 7 allocated clusters for populated 1.44M disk, found {len(clusters)}"
        print(f"Disk Info: Free={free/1024:.1f}KB, Total={total/1024:.1f}KB, Clusters={len(clusters)}")
