# tests/hardware/test_hw_drive_b_write_pytest.py
"""
Hardware tests for write operations on Drive B using a 360K formatted disk.

This test suite verifies the functionality of the DiskController for writing
and deleting files and directories on a physical 5.25" floppy drive.

It requires the 'TEST_DRIVE_B' environment variable to be set to 'true'.
The tests will:
1.  Use Greaseweazle to write a clean, formatted 360K disk image.
2.  Initialize the DiskController with the physical drive.
3.  Perform various write operations (create/write/delete file, create/delete
    directory, write multi-cluster file).
4.  Assert that the operations were successful by reading back data or
    listing directory contents.

WARNING: These tests are DESTRUCTIVE and will modify the contents of the
         floppy disk in the target drive.
"""

import os
from typing import Generator

import pytest
from _pytest.config import Config

from fatfloppy.core.controller import DiskController
from .conftest import EMPTY_360K_IMG, GW_FORMAT_360K, _run_gw_write

# --- Test Configuration ---

TARGET_DRIVE: str = 'B'
TARGET_FORMAT_KEY: str = '360K'
TARGET_DRIVE_SIZE: str = "5.25"
PROFILE_NAME: str = 'ibm_5.25_360k'

# Apply markers to all tests in this module.
pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif(
        os.getenv('TEST_DRIVE_B', 'false').lower() != 'true',
        reason=(
            "Requires TEST_DRIVE_B='true' environment variable to be set. "
            f"These tests target Drive {TARGET_DRIVE} with a {TARGET_FORMAT_KEY} disk."
        )
    )
]


# --- Fixtures ---

@pytest.fixture(scope="class")
def prepared_controller(pytestconfig: Config) -> Generator[DiskController, None, None]:
    """
    Class-scoped fixture to prepare a physical floppy disk for write tests.

    This fixture performs the following setup actions:
    1.  Warns the user that the disk will be modified.
    2.  Writes a blank, formatted 360K disk image to the floppy in Drive B
        using the Greaseweazle tool.
    3.  Initializes a DiskController, specifying the correct format profile,
        and opens the physical drive.
    4.  Verifies that the filesystem is valid and ready for write operations.

    After all tests in the class have run, it performs teardown by closing
    the connection to the disk.

    Args:
        pytestconfig: The pytest configuration object, used to get command-line
                      options.

    Yields:
        An initialized and ready-to-use DiskController instance.
    """
    print(f"\n--- Setting up Hardware Test Class for Drive {TARGET_DRIVE} ({TARGET_FORMAT_KEY} Write) ---")
    print("INFO: Ensure the floppy in Drive B is suitable for writing.")
    print("      This disk WILL BE MODIFIED.")
    auto_mode: bool = pytestconfig.getoption("--hw-auto")

    # Prepare the disk with a clean, formatted image.
    write_success = _run_gw_write(
        TARGET_DRIVE,
        EMPTY_360K_IMG,
        GW_FORMAT_360K,
        f"Empty {TARGET_FORMAT_KEY} for Write Tests",
        auto_mode
    )
    if not write_success:
        pytest.skip(f"Skipping tests for Drive {TARGET_DRIVE} due to write failure.", allow_module_level=True)

    print(f"Attempting to open Drive {TARGET_DRIVE}...")
    controller = DiskController()
    gw_device: str | None = os.environ.get('GW_DEVICE', None)
    format_info_to_pass = {"format_name": PROFILE_NAME}

    success = controller.open_disk(
        source=gw_device,
        disk_type="physical",
        drive_letter=TARGET_DRIVE,
        drive_size=TARGET_DRIVE_SIZE,
        format_info=format_info_to_pass
    )

    if not success:
        controller.close_disk()
        pytest.skip(
            f"Failed to open physical drive {TARGET_DRIVE} after preparation. Check connection/disk.",
            allow_module_level=True
        )

    if not controller.filesystem or not controller.filesystem.is_valid():
        controller.close_disk()
        pytest.skip(
            f"Filesystem not valid after opening Drive {TARGET_DRIVE} with format {PROFILE_NAME}",
            allow_module_level=True
        )

    print(f"Drive {TARGET_DRIVE} opened successfully with format '{PROFILE_NAME}'.")
    yield controller
    print(f"\n--- Tearing down Hardware Test Class for Drive {TARGET_DRIVE} (Write) ---")
    controller.close_disk()
    print(f"Drive {TARGET_DRIVE} connection closed.")


# --- Test Class ---

@pytest.mark.usefixtures("prepared_controller")
class TestHardwareDriveBWrite:
    """
    A collection of write and delete tests for a 360K floppy in Drive B.

    These tests assume the drive has been prepared by the `prepared_controller`
    fixture with a clean, formatted filesystem. Each test is responsible for
    creating and cleaning up its own files/directories.
    """

    def _cleanup_item(self, controller: DiskController, path: str) -> None:
        """
        Robustly attempts to delete a file or directory for test cleanup.

        Checks for the existence of the item before attempting deletion to
        avoid unnecessary errors. Logs the outcome of the cleanup attempt.

        Args:
            controller: The active DiskController instance.
            path: The full path of the item to delete (e.g., "/MYFILE.TXT").
        """
        if not controller or not controller.filesystem:
            print(f"WARN: Controller or Filesystem not available during cleanup of {path}, skipping.")
            return

        print(f"Attempting cleanup: delete '{path}'")
        try:
            parent_path, name = controller.filesystem._split_path(path)
            parent_content = controller.list_directory(parent_path)
            if parent_content is None:
                print(f"WARN: Cleanup could not list parent directory '{parent_path}' for '{path}'.")
                return

            item_exists = any(item['name'].upper() == name.upper() for item in parent_content)

            if item_exists:
                deleted = controller.delete_item(path)
                if deleted:
                    print(f"Cleanup: Successfully deleted '{path}'")
                else:
                    print(f"WARN: Cleanup delete command failed for {path}")
            else:
                print(f"Cleanup: Item '{path}' not found in listing, assuming already deleted.")

        except Exception as e:
            print(f"WARN: Exception during cleanup for {path}: {e}")

    def test_01_hw_B_create_write_read_delete_file(self, prepared_controller: DiskController) -> None:
        """Verify the full lifecycle of a file: create, write, read, and delete."""
        print("\nRunning: HW Write: Create, write, read, delete file on Drive B")
        controller = prepared_controller
        filepath: str = "/WRITE_B.TMP"
        content: bytes = b"360KB hardware test " * 10

        try:
            print(f"  Writing {filepath}...")
            write_ok = controller.write_file(filepath, content)
            assert write_ok, f"write_file failed for {filepath}"

            print(f"  Reading back {filepath}...")
            read_content = controller.read_file(filepath)
            assert read_content is not None, f"read_file returned None for {filepath}"
            assert read_content == content, f"Read content mismatch for {filepath}"
        finally:
            self._cleanup_item(controller, filepath)
            read_after_delete = controller.read_file(filepath)
            assert read_after_delete is None, f"File '{filepath}' still readable after deletion attempt."
            print(f"Verified deletion of {filepath} (read returned None)")

    def test_02_hw_B_create_delete_dir(self, prepared_controller: DiskController) -> None:
        """Verify the lifecycle of a directory: create and delete."""
        print("\nRunning: HW Write: Create and delete directory on Drive B")
        controller = prepared_controller
        dirpath: str = "/B_DIR"

        try:
            print(f"  Creating {dirpath}...")
            create_ok = controller.create_directory(dirpath)
            assert create_ok, f"create_directory failed for {dirpath}"

            root_list = controller.list_directory("/")
            assert root_list is not None, "Failed to list root directory after creation."
            assert any(e['name'] == 'B_DIR' and e['is_dir'] for e in root_list), f"{dirpath} not found after creation"
            print(f"Verified creation of {dirpath}")
        finally:
            self._cleanup_item(controller, dirpath)
            root_list_after = controller.list_directory("/")
            assert root_list_after is not None, "Failed to list root directory after deletion."
            assert not any(e['name'] == 'B_DIR' for e in root_list_after), f"{dirpath} still found after deletion attempt"
            print(f"Verified deletion of {dirpath}")

    def test_03_hw_B_write_multicluster_file(self, prepared_controller: DiskController) -> None:
        """Verify writing a file large enough to span multiple clusters."""
        print("\nRunning: HW Write: Create file spanning multiple clusters on Drive B")
        controller = prepared_controller
        filepath: str = "/MULTI_B.BIN"
        # 360k has 1024 bytes/cluster (512 bytes/sector * 2 sectors/cluster)
        content: bytes = b"360K Cluster Data! " * 160  # Approx 2720 bytes > 2 clusters
        assert len(content) > 1024 * 2, "Content length should be > 2 clusters (360k)"

        try:
            print(f"  Writing {filepath} ({len(content)} bytes)...")
            write_ok = controller.write_file(filepath, content)
            assert write_ok, f"write_file failed for {filepath}"

            print(f"  Reading back {filepath}...")
            read_content = controller.read_file(filepath)
            assert read_content == content, f"Read content mismatch for {filepath}"

            fs = controller.filesystem
            assert fs is not None, "Filesystem object is missing"

            list_dir_result = controller.list_directory("/")
            print(f"DEBUG: list_directory('/') returned: {list_dir_result}")
            assert list_dir_result is not None, "Failed to list directory after write."
            found_entry = next((item for item in list_dir_result if item.get('name', '').upper() == 'MULTI_B.BIN'), None)

            assert found_entry is not None, f"'{filepath}' not found in listing after write"
            print(f"DEBUG: Found entry for '{filepath}': {found_entry}")
            file_size = found_entry.get('size', -1)
            assert file_size == len(content), f"Reported file size ({file_size}) does not match written content size ({len(content)})"

            if 'starting_cluster' in found_entry:
                start_cluster = found_entry['starting_cluster']
                if start_cluster == 0:
                    pytest.fail(f"File '{filepath}' has starting_cluster 0 despite writing {len(content)} bytes.")

                print(f"DEBUG: Found starting cluster {start_cluster} for '{filepath}'")
                assert fs.allocation_unit_size > 0, "Cluster size is zero"
                expected_clusters = (len(content) + fs.allocation_unit_size - 1) // fs.allocation_unit_size

                try:
                    cluster_chain = fs._get_cluster_chain(start_cluster)
                    assert len(cluster_chain) == expected_clusters, f"Expected {expected_clusters} clusters, found {len(cluster_chain)}"
                    print(f"Verified file uses {len(cluster_chain)} clusters.")
                except Exception as e:
                    pytest.fail(f"Error getting cluster chain for cluster {start_cluster}: {e}")
            else:
                pytest.fail(f"File '{filepath}' is missing 'starting_cluster' key despite content length {len(content)}.")
        finally:
            self._cleanup_item(controller, filepath)
            read_after_delete = controller.read_file(filepath)
            assert read_after_delete is None, f"File '{filepath}' still readable after deletion attempt."
            print(f"Verified deletion of {filepath} (read returned None)")
